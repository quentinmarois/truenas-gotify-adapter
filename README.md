# TrueNAS Gotify Adapter

Send TrueNAS alerts to Gotify with priorities that follow the native TrueNAS alert severity.

The adapter supports two modes:

- **Native mode (recommended):** connects to the TrueNAS JSON-RPC WebSocket API, subscribes to `alert.list`, and maps each alert's `level` to a Gotify priority.
- **Legacy webhook mode:** keeps compatibility with the original adapter by exposing a Slack-compatible webhook endpoint and forwarding its text to Gotify.

Native mode requires TrueNAS 25.04 or later.

## Priority mapping

| TrueNAS level | Gotify priority |
| --- | ---: |
| `INFO` | 1 |
| `NOTICE` | 2 |
| `WARNING` | 4 |
| `ERROR` | 6 |
| `CRITICAL` | 8 |
| `ALERT` | 9 |
| `EMERGENCY` | 10 |
| Resolved alert | 2 |

Priorities can be overridden with environment variables such as `GOTIFY_PRIORITY_WARNING=5` or `GOTIFY_PRIORITY_CRITICAL=10`.

## Native mode

Create a dedicated TrueNAS API key. The minimum role needed by the alert API is `ALERT_LIST_READ`. Using a dedicated service account/privilege instead of a full-administrator API key is recommended.

`TRUENAS_USERNAME` should be the user that owns the API key. The adapter uses `auth.login_ex` with the `API_KEY_PLAIN` mechanism. If the username is omitted it falls back to the deprecated `auth.login_with_api_key` method for compatibility, but that method is removed in newer TrueNAS API versions.

### Docker Compose

```yaml
services:
  truenas-gotify-adapter:
    container_name: truenas-gotify-adapter
    image: ghcr.io/quentinmarois/truenas-gotify-adapter:main
    restart: unless-stopped
    environment:
      GOTIFY_URL: https://gotify.example.com/
      GOTIFY_TOKEN: your-gotify-app-token
      TRUENAS_URL: https://truenas.example.com/
      TRUENAS_USERNAME: gotify-alerts
      TRUENAS_API_KEY: your-truenas-api-key
      TRUENAS_VERIFY_CERT: "true"
```

`TRUENAS_URL` accepts `http://`, `https://`, `ws://`, or `wss://`. The adapter automatically uses the `/api/current` WebSocket endpoint.

The adapter takes an initial snapshot of existing alerts without notifying for them. It then:

- sends `ADDED` alerts using their TrueNAS severity;
- sends `CHANGED` alerts only when the mapped priority changes, avoiding repeated notifications for bookkeeping updates;
- re-queries `alert.list` after `REMOVED` events so it can identify the cleared alert and send a low-priority resolved notification;
- reconnects automatically if the TrueNAS WebSocket connection drops.

### Running as a TrueNAS custom app

If the adapter itself runs on TrueNAS and you point `TRUENAS_URL` at `127.0.0.1`, enable host networking so the container can reach the host's Web UI/API socket. If you use a routable hostname/IP instead, host networking is not inherently required.

## Legacy Slack-compatible webhook mode

If `TRUENAS_URL` and `TRUENAS_API_KEY` are not set, legacy mode is enabled automatically and behaves like the original project.

```yaml
services:
  truenas-gotify-adapter:
    container_name: truenas-gotify-adapter
    image: ghcr.io/quentinmarois/truenas-gotify-adapter:main
    restart: unless-stopped
    environment:
      GOTIFY_URL: https://gotify.example.com/
      GOTIFY_TOKEN: your-gotify-app-token
      GOTIFY_PRIORITY_LEGACY: "0"
    network_mode: host
```

In TrueNAS:

1. Open **System → Alert Settings → Add**.
2. Select **Slack**.
3. Set the webhook URL to `http://localhost:31662`.
4. Send a test alert and save.

The legacy Slack payload does not contain TrueNAS severity, so it cannot provide dynamic priorities. `GOTIFY_PRIORITY_LEGACY` defaults to `0` to preserve the original behavior; set it to a fixed value such as `5` if desired.

To expose the legacy webhook while native mode is also configured, set `ENABLE_LEGACY_WEBHOOK=true`. Normally this should remain disabled in native mode to avoid duplicate notifications.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `GOTIFY_URL` | required | Gotify base URL; `/message` is appended automatically. |
| `GOTIFY_TOKEN` | required | Gotify application token. |
| `GOTIFY_VERIFY_CERT` | `true` | Verify Gotify TLS certificates. |
| `VERIFY_CERT` | `true` | Backward-compatible alias used as the default for `GOTIFY_VERIFY_CERT`. |
| `TRUENAS_URL` | unset | TrueNAS URL; setting this with `TRUENAS_API_KEY` enables native mode. |
| `TRUENAS_USERNAME` | unset | Owner of the TrueNAS API key; recommended and required for future API compatibility. |
| `TRUENAS_API_KEY` | unset | TrueNAS API key. |
| `TRUENAS_VERIFY_CERT` | `true` | Verify TrueNAS TLS certificates. |
| `ENABLE_LEGACY_WEBHOOK` | auto | Defaults to `false` in native mode and `true` otherwise. |
| `LISTEN_HOST` | `127.0.0.1` | Legacy webhook listen address. |
| `PORT` | `31662` | Legacy webhook port. |
| `GOTIFY_PRIORITY_INFO` | `1` | Priority for INFO alerts. |
| `GOTIFY_PRIORITY_NOTICE` | `2` | Priority for NOTICE alerts. |
| `GOTIFY_PRIORITY_WARNING` | `4` | Priority for WARNING alerts. |
| `GOTIFY_PRIORITY_ERROR` | `6` | Priority for ERROR alerts. |
| `GOTIFY_PRIORITY_CRITICAL` | `8` | Priority for CRITICAL alerts. |
| `GOTIFY_PRIORITY_ALERT` | `9` | Priority for ALERT alerts. |
| `GOTIFY_PRIORITY_EMERGENCY` | `10` | Priority for EMERGENCY alerts. |
| `GOTIFY_PRIORITY_RESOLVED` | `2` | Priority for cleared alerts. |
| `GOTIFY_PRIORITY_LEGACY` | `0` | Fixed priority used by legacy webhook mode. |
| `LOG_LEVEL` | `INFO` | Python logging level. |

## Development

```sh
python -m unittest discover -s tests
python -m py_compile truenas-gotify.py
```

## Credits

Forked from [ZTube/truenas-gotify-adapter](https://github.com/ZTube/truenas-gotify-adapter), which provided the original Slack-webhook-to-Gotify bridge.
