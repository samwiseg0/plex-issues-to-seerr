# plex-issues-to-seerr

plex-issues-to-seerr copies Plex "Report Issue" reports into Seerr issues. It files each issue as the user who reported it. It posts the Seerr link back on the Plex report, and posts again when you resolve the issue.

Plex keeps issue reports on plex.tv, never on your server. Plex offers no webhook for them. So the tool polls plex.tv every few minutes.

It is one Python file with no dependencies outside the standard library.

## How a report flows

1. A user reports an issue on a movie or an episode in a Plex app.
2. The next poll reads the report from plex.tv.
3. The poller looks up the item on your Plex server and reads its TMDB, IMDb or TVDB id.
4. It files a Seerr issue for that title as the reporter's Seerr user. The issue text holds the report text, the reporter, the date and a `Plex report <id>.` tag.
5. It comments `Tracked at https://seerr.example.com/issues/42` on the Plex report.
6. You resolve the issue in Seerr.
7. The next poll comments on the Plex report again. The comment is the newest Seerr reply that the reporter did not write. With no reply, it is `Resolved. Thanks for the report.`

A reopened issue that you resolve again gets another comment.

The poller cannot mirror some reports. Examples are a music track, an item deleted from Plex, or a title Seerr does not know. It posts each of these once to a Discord webhook as an embed. With no webhook, it logs a `WARNING` line instead.

### Issue type

The poller guesses the Seerr issue type from words in the report. The first row that matches wins.

| Seerr type | Words in the report (any case) |
|---|---|
| Subtitles | subtitle, caption, sub, subs |
| Audio | audio, sound, language, dub, any word that starts with "eng" |
| Video | show, episode, movie, film, skip, playback, freeze, buffer, video, picture |
| Other | no match |

## Limits

- plex.tv has no API to resolve or close a report. The poller can only comment on it.
- Seerr files an issue against its own media row for the title. Enable your Plex libraries in Seerr's Plex settings. Seerr's library scan then gives every title a media row.
- The item must still exist on your Plex server. The poller reads the title's ids from the server.
- Reporters map to Seerr users by Plex username. The poller files an unmatched reporter's issue as the Seerr user that owns the API key. The issue text still names the reporter.
- The plex.tv reports API is not documented. Plex can change it without notice.
- A report deleted on plex.tv drops out of the report list. The poller then marks it `"gone": "not listed"` and stops commenting on it. A later poll that lists the report again removes the mark.
- A crash between a Discord post and the state save posts that report to Discord again.
- plex.tv pages the report list by the report date in milliseconds. Two reports with the same millisecond at a page boundary can be missed, and a mirrored one marked `gone` until a later poll lists it.

## Requirements

- A Plex token of the account that owns the server. See [Finding an authentication token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
- Seerr 3.4 or newer. Older Seerr ignores the reporter and files every issue as the API key's user. Overseerr is not supported.
- A Seerr API key, from Settings, General.
- Seerr users imported from Plex, so each user has a Plex username.
- Network access to plex.tv, community.plex.tv, your Plex server, Seerr, and Discord if you use it.

## Quick start with Docker Compose

```sh
mkdir plex-issues-to-seerr && cd plex-issues-to-seerr
curl -o docker-compose.yml https://raw.githubusercontent.com/samwiseg0/plex-issues-to-seerr/main/docker-compose.example.yml
curl -o .env https://raw.githubusercontent.com/samwiseg0/plex-issues-to-seerr/main/.env.example
```

Edit `.env`. Check the settings with a dry run. Then start the service.

```sh
docker compose run --rm plex-issues-to-seerr --dry-run
docker compose up -d
docker compose logs -f
```

The image is `ghcr.io/samwiseg0/plex-issues-to-seerr` for linux/amd64 and linux/arm64. It runs as UID 10001 and keeps its files in the `/data` volume.

In a container, `POLL_INTERVAL` must stay 60 or more, because 0 means one poll and exit. An empty `POLL_INTERVAL` uses the image default of 300.

## Configuration

| Variable | Required | Default | What it does |
|---|---|---|---|
| `PLEX_TOKEN` | yes | | plex.tv token of the server owner's account. |
| `PMS_URL` | yes | | Plex Media Server base URL, for example `http://plex:32400`. |
| `SEERR_URL` | yes | | Seerr base URL the poller calls. |
| `SEERR_API_KEY` | yes | | Seerr API key. |
| `SEERR_PUBLIC_URL` | no | `SEERR_URL` | Seerr URL in the link on the Plex report. Set it when the poller reaches Seerr on an internal address. |
| `PLEX_OWNER` | no | the token's account | plex.tv username the reports are sent to. |
| `DISCORD_WEBHOOK_URL` | no | unset | Webhook for reports the poller cannot mirror. Unset logs them instead. |
| `IMPORT_DAYS` | no | `0` | The first run imports reports from this many days back. |
| `POLL_INTERVAL` | no | `300` in the image | Seconds between polls, 60 to 86400. Outside the image, unset, empty or `0` polls once and exits non-zero when the poll failed. |
| `STATE_FILE` | no | `/data/state.json` in the image | Path of the state file. Outside the image the default is `plex_issues_state.json` in the working directory. The poller creates a missing directory. |
| `HEARTBEAT_FILE` | no | `/data/heartbeat` in the image | File rewritten after every successful poll. |

Each URL must use http or https and must not carry a user name or password.

## First run and backfill

The first run stores a start date in the state file. By default it mirrors only reports made after that date.

To import older reports, set `IMPORT_DAYS` before the first run. Run `--dry-run` first. It prints what the poller would do with each report and writes nothing. Each report it cannot mirror posts to Discord once. Check those lines before the real run.

Before it files a report, the poller looks for a Seerr issue that carries the report's `Plex report <id>.` tag. When it finds one, it rebuilds the report's record from that issue. So a lost state file never files a report twice.

The poller skips a report with no such issue when the report carries the poller's own comment on plex.tv. That comment is exactly `Tracked at <url>/issues/<number>`. A comment from anyone else does not count.

`IMPORT_DAYS` has no effect once the state file holds a start date.

## The state file

The state file is JSON. It holds the start date and one record per report under `"reports"`, keyed by the plex.tv report id.

Stop the poller before you edit the file. A running poll rewrites it. The image carries `vi` for the edit.

```sh
docker compose stop
docker compose run --rm --entrypoint vi plex-issues-to-seerr /data/state.json
docker compose run --rm --entrypoint python plex-issues-to-seerr -m json.tool /data/state.json
docker compose start
```

The last `run` checks that the file is still valid JSON.

To skip a new report that fails every poll, add the record below under `"reports"`. The `ERROR` line in the log names the report id.

```json
"<report id>": {"skipped": "by hand"}
```

To stop watching a mirrored report that fails every poll, add `"gone": true` to its record. The poller never removes a `true` value. It removes only the `"not listed"` value it writes itself, when plex.tv lists the report again.

The poller refuses to run on an empty, corrupt or non-object state file. Restore the file from a backup. Or replace it with `{}` and set `IMPORT_DAYS` to cover the gap. Unmatched reports in that window then post to Discord again. Mirrored reports find their Seerr issues by tag, and the poller watches them again. It reads the Plex comments, so it posts no second `Tracked at` comment. It posts no second resolve comment when the Plex report already carries the issue's current resolve reply.

## Healthcheck

The poller deletes the heartbeat file at startup. It rewrites the file after each successful poll. A poll fails when the Plex server or Seerr rejects the URL or key, so a bad setting also stops the heartbeat.

The image's healthcheck marks the container unhealthy when the heartbeat is missing or older than 3 x `POLL_INTERVAL`. It allows 15 minutes for the first polls. Docker reports the status only and does not restart the container.

```sh
docker inspect --format '{{.State.Health.Status}}' <container>
```

## Running without Docker

The script needs Python 3.11 or newer and nothing else.

```sh
set -a; . ./.env; set +a
python3 plex_issues.py --dry-run
python3 plex_issues.py
```

With `POLL_INTERVAL` unset, the script polls once and exits. It exits non-zero when the poll failed. A second copy started while one runs exits at once.

To run it from systemd every 5 minutes, use the units in `contrib/systemd`. The service runs as a dynamic user and keeps its state in `/var/lib/plex-issues-to-seerr`. It ignores `POLL_INTERVAL` from the env file.

```sh
sudo install -D -m 0644 plex_issues.py /opt/plex-issues-to-seerr/plex_issues.py
sudo install -m 0600 .env.example /etc/plex-issues-to-seerr.env
sudo cp contrib/systemd/plex-issues-to-seerr.service contrib/systemd/plex-issues-to-seerr.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now plex-issues-to-seerr.timer
```

Edit `/etc/plex-issues-to-seerr.env` before the first timer run. Read the log with `journalctl -u plex-issues-to-seerr`. A failed poll leaves the service in the failed state.

## Tests

```sh
pip install pytest
python -m pytest
```

The tests cover the pure functions and a poll against an in-memory plex.tv, Plex server and Seerr. They make no network calls.

## License

GNU General Public License v3.0 or later (`GPL-3.0-or-later`). See [LICENSE](LICENSE).
