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

// The site's own address is a plain Worker variable, not a constant, so this
// file carries no domain and can live in the public collector repository
// alongside the workflow it triggers.
const SNAPSHOT_FALLBACK = "";

// Only ask for a run if the published snapshot is already older than this. If
// GitHub's own schedule happens to fire, this stays quiet and costs nothing.
const MAX_AGE_MINUTES = 25;

async function snapshotAgeMinutes(env) {
  try {
    const url = (env && env.SNAPSHOT_URL) || SNAPSHOT_FALLBACK;
    if (!url) return null;
    const r = await fetch(url + "?cron=1", { cf: { cacheTtl: 0 } });
    if (!r.ok) return null;
    const d = await r.json();
    const t = Date.parse(d && d.at);
    if (!isFinite(t)) return null;
    return (Date.now() - t) / 60000;
  } catch (e) {
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
      const age = await snapshotAgeMinutes(env);
      if (age !== null && age < MAX_AGE_MINUTES) {
        console.log("skip — snapshot is " + Math.round(age) + " minutes old");
        return;
      }
      const r = await dispatch(env);
      console.log("dispatch " + r.status + " — snapshot age " +
                  (age === null ? "unknown" : Math.round(age) + " min"));
    })());
  },

  // Read only, on purpose. This address is public, and a "run now" here would let
  // anyone spin the collector. Use the dashboard's own scheduled-event test, or
  // the workflow's Run button, to force one.
  async fetch(request, env) {
    const age = await snapshotAgeMinutes(env);
    return new Response(JSON.stringify({
      ok: true,
      snapshotAgeMinutes: age === null ? null : Math.round(age),
      dispatchesWhenOlderThan: MAX_AGE_MINUTES
    }, null, 1), { headers: { "content-type": "application/json; charset=utf-8" } });
  }
};
