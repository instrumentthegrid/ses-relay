"""SMTP in, Amazon SES API out.

Accepts mail from trusted local clients and hands the raw message to SES
SendRawEmail. Credentials come from the default AWS chain (EC2 instance role,
ECS task role, EKS pod identity, ...), so there is no long-lived SMTP password
or IAM access key to rotate.

It is deliberately not an open relay: clients must come from ALLOWED_CLIENTS,
and both the envelope sender and every header From address must be in
ALLOWED_FROM. Pair it with an IAM policy that pins ses:FromAddress too.
"""

import asyncio
import ipaddress
import logging
import os
import signal
import threading
from email.parser import BytesHeaderParser
from email.policy import default as default_policy
from email.utils import getaddresses

import boto3
from aiosmtpd.controller import Controller
from botocore.exceptions import ClientError

# Loopback plus RFC 1918 and IPv6 ULA: typical for a docker network or VPC.
DEFAULT_ALLOWED_CLIENTS = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"

# SES errors that retrying the same message will never fix. Anything else
# (throttling, 5xx, missing credentials, IAM being fixed) gets a 4xx so the
# client queues and retries instead of dropping mail.
PERMANENT_SES_ERRORS = {"MessageRejected", "InvalidParameterValue", "MailFromDomainNotVerifiedException"}

log = logging.getLogger("ses-relay")


def parse_senders(value):
    """Split ALLOWED_FROM into exact addresses and @domain entries."""
    addresses, domains = set(), set()
    for entry in (e.strip().lower() for e in value.split(",")):
        if entry.startswith("@"):
            domains.add(entry[1:])
        elif entry:
            addresses.add(entry)
    if not addresses and not domains:
        raise SystemExit("ALLOWED_FROM must list at least one address or @domain")
    return addresses, domains


def parse_networks(value):
    return [ipaddress.ip_network(n.strip()) for n in value.split(",") if n.strip()]


class Relay:
    def __init__(self, ses, allowed_from, allowed_clients, configuration_set=None):
        self.ses = ses
        self.addresses, self.domains = allowed_from
        self.networks = allowed_clients
        self.configuration_set = configuration_set

    def sender_allowed(self, address):
        address = address.lower()
        return address in self.addresses or address.rpartition("@")[2] in self.domains

    def client_allowed(self, peer):
        ip = ipaddress.ip_address(peer[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in net for net in self.networks)

    async def handle_MAIL(self, server, session, envelope, address, mail_options):
        if not self.client_allowed(session.peer):
            log.warning("rejected client %s", session.peer[0])
            return "550 5.7.1 client not allowed"
        if not self.sender_allowed(address):
            log.warning("rejected envelope sender %r from %s", address, session.peer[0])
            return "550 5.7.1 sender not allowed"
        envelope.mail_from = address
        envelope.mail_options.extend(mail_options)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        headers = BytesHeaderParser(policy=default_policy).parsebytes(envelope.original_content)
        header_from = [addr for _, addr in getaddresses(headers.get_all("From", []))]
        if not header_from or not all(self.sender_allowed(a) for a in header_from):
            log.warning("rejected header From %r from %s", header_from, session.peer[0])
            return "550 5.7.1 From header not allowed"

        params = {
            "Source": envelope.mail_from,
            "Destinations": envelope.rcpt_tos,  # envelope recipients, so Bcc works
            "RawMessage": {"Data": envelope.original_content},
        }
        if self.configuration_set:
            params["ConfigurationSetName"] = self.configuration_set
        try:
            resp = await asyncio.to_thread(self.ses.send_raw_email, **params)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            log.error("SES rejected %s -> %s: %s", envelope.mail_from, envelope.rcpt_tos, e)
            if code in PERMANENT_SES_ERRORS:
                return f"554 5.0.0 SES {code}"
            return "451 4.3.0 SES temporarily unavailable, try again later"
        except Exception:
            log.exception("SES send failed for %s -> %s", envelope.mail_from, envelope.rcpt_tos)
            return "451 4.3.0 SES temporarily unavailable, try again later"
        log.info("sent %s -> %s id=%s", envelope.mail_from, envelope.rcpt_tos, resp["MessageId"])
        return "250 OK"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # aiosmtpd logs every SMTP command at INFO; our own lines cover what matters.
    logging.getLogger("mail.log").setLevel(logging.WARNING)
    allowed_from = parse_senders(os.environ.get("ALLOWED_FROM", ""))
    allowed_clients = parse_networks(os.environ.get("ALLOWED_CLIENTS") or DEFAULT_ALLOWED_CLIENTS)
    host = os.environ.get("LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("LISTEN_PORT", "2525"))
    # SES v1 SendRawEmail caps a message at 10 MB.
    max_bytes = int(os.environ.get("MAX_MESSAGE_BYTES", str(10 * 1024 * 1024)))

    relay = Relay(
        # boto3 itself only reads AWS_DEFAULT_REGION; honor AWS_REGION like the other SDKs do.
        ses=boto3.client("ses", region_name=os.environ.get("AWS_REGION") or None),
        allowed_from=allowed_from,
        allowed_clients=allowed_clients,
        configuration_set=os.environ.get("SES_CONFIGURATION_SET") or None,
    )
    controller = Controller(relay, hostname=host, port=port, data_size_limit=max_bytes)
    controller.start()
    log.info(
        "listening on %s:%d, region %s, senders %s, clients %s",
        host, port, relay.ses.meta.region_name,
        sorted(relay.addresses) + sorted("@" + d for d in relay.domains),
        [str(n) for n in allowed_clients],
    )

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    controller.stop()


if __name__ == "__main__":
    main()
