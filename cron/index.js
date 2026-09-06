// Cloudflare Worker: ask GitHub to run the bathing water collector.
//
// WHY THIS EXISTS. The collector runs on GitHub Actions because it parses about
// 18,600 outfall records and a free Cloudflare Worker gets 10ms of CPU. But
// GitHub treats `schedule:` as best effort and drops most of them: asking four
// times an hour produced roughly one run every five hours, while every run that
// did fire succeeded. The site was two to five hours behind and saying so.
//
// `workflow_dispatch` is not best effort — it fires at once. Cloudflare's cron
// triggers are reliable. So this is the reliable half asking the unreliable half
// to get on with it.
//
// Deploy separately from the Pages project: Pages Functions cannot have cron
// triggers, which is the whole reason this is its own Worker.
//
// SECRET: GITHUB_TOKEN — a fine-grained personal access token scoped to the one
// public repository below, with Actions: read and write and nothing else. It is
// a Worker secret and must never be written into this file.

const REPO = "Leiglow/swim-collector";
const WORKFLOW = "swim-collect.yml";

// How recently the collector last SUCCEEDED, asked of GitHub rather than of the
// site. The first version read the published snapshot instead, which worked for
// twenty minutes and then returned null on every call: a Worker fetching a site
// on its own Cloudflare account is an awkward path, and it is the wrong question
// anyway. GitHub already knows when the job last ran, the token can read it, and
// nothing about that answer depends on the website being reachable from inside
// Cloudflare.

// Only ask for a run if the published snapshot is already older than this. If
// GitHub's own schedule happens to fire, this stays quiet and costs nothing.
const MAX_AGE_MINUTES = 25;

// Set by lastRunMinutes(), read by both handlers. A Worker isolate is short
// lived and this is deliberately not state that outlives one request.
let LAST_READ = "not tried";

// WHETHER THE TOKEN STILL WORKS, told apart from every other reason the read
// might fail. The fine-grained token this Worker carries is scoped to one
// repository and expires; when it did, lastRunMinutes() returned null, the
// Worker dispatched as designed, GitHub answered 401, and the only trace was a
// line in a log nobody reads. The site went eight hours stale saying so on
// every page while this went on quietly failing. Standard 5 applies to the
// plumbing as much as to the pages.
//
// So the read reports WHY it could not answer, and the public endpoint below
// says it out loud, in one curl, without anybody opening a dashboard.
async function lastRunMinutes(env) {
  try {
    const r = await fetch(
      "https://api.github.com/repos/" + REPO + "/actions/workflows/" + WORKFLOW +
      "/runs?status=success&per_page=1",
      {
        headers: {
          "authorization": "Bearer " + env.GITHUB_TOKEN,
          "accept": "application/vnd.github+json",
          "x-github-api-version": "2022-11-28",
          "user-agent": "swim-collector-cron"
        }
      }
    );
    if (!r.ok) {
      LAST_READ = r.status === 401 || r.status === 403
        ? "github refused the token (" + r.status + ") — it has expired or lost "
          + "its Actions permission"
        : "github answered " + r.status;
      return null;
    }
    LAST_READ = "ok";
    const d = await r.json();
    const run = d && d.workflow_runs && d.workflow_runs[0];
    const t = Date.parse(run && run.created_at);
    if (!isFinite(t)) return null;
    return (Date.now() - t) / 60000;
  } catch (e) {
    LAST_READ = "could not reach github";
    return null;                 // cannot tell — treat as due rather than skip
  }
}


async function dispatch(env) {
  return fetch(
    "https://api.github.com/repos/" + REPO + "/actions/workflows/" + WORKFLOW + "/dispatches",
    {
      method: "POST",
      headers: {
        "authorization": "Bearer " + env.GITHUB_TOKEN,
        "accept": "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
        "user-agent": "swim-collector-cron",
        "content-type": "application/json"
      },
      body: JSON.stringify({ ref: "main" })
    }
  );
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      const age = await lastRunMinutes(env);
      if (age !== null && age < MAX_AGE_MINUTES) {
        console.log("skip — collector last ran " + Math.round(age) + " minutes ago");
        return;
      }
      const r = await dispatch(env);
      const body = r.ok ? "" : " — " + (await r.text()).slice(0, 200);
      console.log("dispatch " + r.status + " — last run " +
                  (age === null ? "unknown (" + LAST_READ + ")"
                                : Math.round(age) + " min ago") + body);
    })());
  },

  // Read only, on purpose. This address is public, and a "run now" here would let
  // anyone spin the collector. Use the dashboard's own scheduled-event test, or
  // the workflow's Run button, to force one.
  async fetch(request, env) {
    const age = await lastRunMinutes(env);
    const healthy = age !== null;
    return new Response(JSON.stringify({
      // NOT always true. This said ok:true while the token was dead, which is
      // the one moment a health endpoint has a job to do.
      ok: healthy,
      github: LAST_READ,
      hasToken: !!(env && env.GITHUB_TOKEN),
      collectorLastRanMinutesAgo: age === null ? null : Math.round(age),
      dispatchesWhenOlderThan: MAX_AGE_MINUTES
    }, null, 1), {
      status: healthy ? 200 : 503,
      headers: { "content-type": "application/json; charset=utf-8" }
    });
  }
};
