# ses-relay

A small SMTP server that forwards mail to Amazon SES through the **SES API**,
using the standard AWS credential chain: EC2 instance role, ECS task role, EKS
Pod Identity/IRSA, or `AWS_*` environment variables.

The SES SMTP interface only accepts SMTP credentials derived from an IAM user's
long-lived access key, so every app that speaks SMTP to SES ends up holding a
static secret that needs rotating. The SES API accepts temporary role
credentials. ses-relay sits next to the app, speaks SMTP to it, and calls
`SendRawEmail` with whatever role the host or task already has. The app is
configured with no SMTP password at all.

About 150 lines of Python on [aiosmtpd](https://github.com/aio-libs/aiosmtpd)
and boto3, in a Chainguard image that runs as nonroot and has no shell.

## Security model

It is meant to be reachable only by trusted local clients (containers on the
same Docker network, pods in the same namespace). It has no SMTP AUTH and no
TLS, and it is not an open relay:

- Connections from outside `ALLOWED_CLIENTS` are refused at `MAIL FROM`.
  The default is loopback plus private ranges (RFC 1918, IPv6 ULA).
- The envelope sender **and** every `From:` header address must match
  `ALLOWED_FROM`. There is no default; it refuses to start without one.
- The IAM policy should pin the same sender with `ses:FromAddress`, so SES
  enforces it too (see below).

Don't publish its port to the internet. Put it on the app's network instead.

## Quick start

```bash
AWS_REGION=us-east-1 \
ALLOWED_FROM=noreply@example.com \
APP_NETWORK=myapp_default \
docker compose up -d --build
```

Then point the app at SMTP host `ses-relay`, port `2525`, with no
authentication and no TLS. `compose.yaml` attaches the relay to an existing
external network and publishes no ports. It also works as a Portainer git
stack, with the variables set in the stack's environment.

## Configuration

| Variable | Default | |
|---|---|---|
| `ALLOWED_FROM` | *required* | Comma-separated sender addresses. `@example.com` allows a whole domain; prefer exact addresses. |
| `AWS_REGION` | from AWS config | SES region. `AWS_DEFAULT_REGION` also works. |
| `ALLOWED_CLIENTS` | loopback + private ranges | Comma-separated CIDRs allowed to send, e.g. the Docker network's subnet. |
| `SES_CONFIGURATION_SET` | none | Passed as `ConfigurationSetName`, for event publishing. |
| `LISTEN_HOST` / `LISTEN_PORT` | `0.0.0.0` / `2525` | |
| `MAX_MESSAGE_BYTES` | 10 MiB | The `SendRawEmail` limit. Advertised via `SIZE`. |

SES errors that retrying can't fix (`MessageRejected`,
`MailFromDomainNotVerifiedException`, `InvalidParameterValue`) return `554`.
Everything else, including throttling and missing credentials, returns `451`,
so the client queues and retries instead of dropping mail.

## IAM

Grant the role only this:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ses:SendRawEmail",
      "Resource": "arn:aws:ses:REGION:ACCOUNT_ID:identity/*",
      "Condition": {
        "StringEquals": { "ses:FromAddress": "noreply@example.com" }
      }
    }
  ]
}
```

If you set `SES_CONFIGURATION_SET`, also add
`arn:aws:ses:REGION:ACCOUNT_ID:configuration-set/NAME` to `Resource`.

### On EC2

Containers on a Docker bridge network are one network hop further from the
instance metadata service. With IMDSv2 they need a PUT response hop limit of
at least 2:

```bash
aws ec2 modify-instance-metadata-options --instance-id i-0123456789abcdef0 \
  --http-tokens required --http-put-response-hop-limit 2
```

That makes the instance role usable by **every** container on the host, not
only this one. That's one more reason to keep the role down to the
single-sender policy above.

## Development

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The tests stub SES with botocore's `Stubber`. No AWS account is needed.
