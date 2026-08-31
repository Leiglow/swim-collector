# The cron Worker

`cron/index.js` runs on Cloudflare, not here. It exists because the collector's
own `schedule:` in GitHub Actions is unreliable — see the comment at the top of
the file for the measurements — and `workflow_dispatch` is not.

It needs two things set on the Worker, neither of which belongs in a public
repository:

| name | kind | what |
|---|---|---|
| `SNAPSHOT_URL` | variable | the site's `/swim/data` address |
| `GITHUB_TOKEN` | secret | fine-grained token, this repo only, Actions: read and write |

The schedule itself is in `wrangler.jsonc`, so it is version-controlled rather
than clicked into a dashboard.
