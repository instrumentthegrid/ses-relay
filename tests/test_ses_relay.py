import ipaddress
import os
import smtplib
import socket
import unittest

import boto3
from aiosmtpd.controller import Controller
from botocore.credentials import CredentialResolver
from botocore.session import get_session
from botocore.stub import ANY, Stubber

import ses_relay

SENDER = "noreply@example.com"
MSG = (
    f"From: App <{SENDER}>\r\nTo: a@example.org\r\nSubject: hi\r\n\r\nbody\r\n"
).encode()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def client_without_credentials():
    # What boto3.client() returns when the credential chain finds nothing.
    session = get_session()
    session.register_component("credential_provider", CredentialResolver([]))
    return session.create_client("ses", region_name="us-west-2")


class RelayTest(unittest.TestCase):
    def start(self, allowed_from=SENDER, allowed_clients="127.0.0.0/8", configuration_set=None,
              ses=None, ses_factory=None):
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
        stubbed = boto3.client("ses", region_name="us-west-2")
        self.stub = Stubber(stubbed)
        self.stub.activate()
        relay = ses_relay.Relay(
            ses=ses or stubbed,
            allowed_from=ses_relay.parse_senders(allowed_from),
            allowed_clients=ses_relay.parse_networks(allowed_clients),
            configuration_set=configuration_set,
            ses_factory=ses_factory or (lambda: stubbed),
        )
        self.port = free_port()
        self.controller = Controller(relay, hostname="127.0.0.1", port=self.port)
        self.controller.start()
        self.addCleanup(self.controller.stop)

    def smtp(self):
        client = smtplib.SMTP("127.0.0.1", self.port)
        self.addCleanup(client.close)
        return client

    def test_relays_with_envelope_recipients_and_configuration_set(self):
        self.start(configuration_set="tracking")
        self.stub.add_response(
            "send_raw_email",
            {"MessageId": "m-1"},
            {
                "Source": SENDER,
                "Destinations": ["a@example.org", "bcc@example.org"],
                "RawMessage": {"Data": ANY},
                "ConfigurationSetName": "tracking",
            },
        )
        self.assertEqual(self.smtp().sendmail(SENDER, ["a@example.org", "bcc@example.org"], MSG), {})
        self.stub.assert_no_pending_responses()

    def test_rejects_envelope_sender_not_allowed(self):
        self.start()
        with self.assertRaises(smtplib.SMTPSenderRefused) as cm:
            self.smtp().sendmail("evil@example.com", ["a@example.org"], MSG)
        self.assertEqual(cm.exception.smtp_code, 550)

    def test_rejects_header_from_not_allowed(self):
        self.start()
        spoofed = MSG.replace(f"From: App <{SENDER}>".encode(), b"From: CEO <ceo@example.com>")
        with self.assertRaises(smtplib.SMTPDataError) as cm:
            self.smtp().sendmail(SENDER, ["a@example.org"], spoofed)
        self.assertEqual(cm.exception.smtp_code, 550)

    def test_rejects_missing_header_from(self):
        self.start()
        with self.assertRaises(smtplib.SMTPDataError) as cm:
            self.smtp().sendmail(SENDER, ["a@example.org"], b"Subject: hi\r\n\r\nbody\r\n")
        self.assertEqual(cm.exception.smtp_code, 550)

    def test_rejects_client_outside_allowed_networks(self):
        self.start(allowed_clients="10.0.0.0/8")
        with self.assertRaises(smtplib.SMTPSenderRefused) as cm:
            self.smtp().sendmail(SENDER, ["a@example.org"], MSG)
        self.assertEqual(cm.exception.smtp_code, 550)

    def test_domain_entry_allows_any_address_in_domain(self):
        self.start(allowed_from="@example.com")
        self.stub.add_response("send_raw_email", {"MessageId": "m-2"})
        self.assertEqual(self.smtp().sendmail(SENDER, ["a@example.org"], MSG), {})

    def test_throttling_is_temporary(self):
        self.start()
        self.stub.add_client_error("send_raw_email", service_error_code="Throttling", http_status_code=400)
        with self.assertRaises(smtplib.SMTPDataError) as cm:
            self.smtp().sendmail(SENDER, ["a@example.org"], MSG)
        self.assertEqual(cm.exception.smtp_code, 451)

    def test_message_rejected_is_permanent(self):
        self.start()
        self.stub.add_client_error("send_raw_email", service_error_code="MessageRejected", http_status_code=400)
        with self.assertRaises(smtplib.SMTPDataError) as cm:
            self.smtp().sendmail(SENDER, ["a@example.org"], MSG)
        self.assertEqual(cm.exception.smtp_code, 554)

    def test_replaces_client_created_without_credentials(self):
        # As when the instance role is attached after the relay starts.
        self.start(ses=client_without_credentials())
        self.stub.add_response("send_raw_email", {"MessageId": "m-3"})
        self.assertEqual(self.smtp().sendmail(SENDER, ["a@example.org"], MSG), {})
        self.stub.assert_no_pending_responses()

    def test_missing_credentials_are_temporary(self):
        self.start(ses=client_without_credentials(), ses_factory=client_without_credentials)
        with self.assertLogs("ses-relay", "ERROR"), self.assertRaises(smtplib.SMTPDataError) as cm:
            self.smtp().sendmail(SENDER, ["a@example.org"], MSG)
        self.assertEqual(cm.exception.smtp_code, 451)


class ParsingTest(unittest.TestCase):
    def test_parse_senders(self):
        self.assertEqual(
            ses_relay.parse_senders(" A@Example.com , @Example.org ,"),
            ({"a@example.com"}, {"example.org"}),
        )

    def test_empty_allowed_from_refuses_to_start(self):
        with self.assertRaises(SystemExit):
            ses_relay.parse_senders(" , ")

    def test_ipv4_mapped_client_matches_ipv4_network(self):
        relay = ses_relay.Relay(None, ({"x@y"}, set()), [ipaddress.ip_network("172.16.0.0/12")])
        self.assertTrue(relay.client_allowed(("::ffff:172.19.0.5", 1234)))
        self.assertFalse(relay.client_allowed(("::ffff:8.8.8.8", 1234)))


if __name__ == "__main__":
    unittest.main()
