/**
 * Circuit newswire pinger — the one piece of the JI Newswire worth copying exactly.
 *
 * The JI Newswire's scheduling is the part of it that works better than ours: a
 * time-driven Apps Script trigger fires on Google's servers, reliably, forever. GitHub's
 * own `schedule` is throttled on public repos and was measured firing every two to six
 * hours instead of every fifteen minutes, which with a 30-minute publish window means
 * stories are missed outright rather than delayed.
 *
 * So this script copies JI's clock and nothing else. It does no fetching, no scoring and
 * no posting — it just tells the GitHub workflow to run. Everything that made the JI
 * Newswire hard to debug (logs visible only in this editor, the 9KB property ceiling,
 * the runtime limit) stays out of the picture, because none of that logic lives here.
 *
 * SETUP
 *   1. script.google.com → New project → paste this file in, replacing Code.gs.
 *   2. Project Settings → Script Properties → Add:
 *        GH_TOKEN = a GitHub fine-grained token with Actions: Read and write
 *                   on LevelThreeLabsO/circuit-newswire
 *   3. Run `pingNewswire` once manually and approve the authorisation prompt.
 *      A successful run logs "dispatched 204".
 *   4. Triggers (clock icon) → Add Trigger → pingNewswire → Time-driven →
 *      Minutes timer → Every 15 minutes.
 *
 * Install it under an account that will outlive the project. The JI original was
 * orphaned when a colleague left and their trigger kept firing, duplicating every post
 * with no way to delete it from another account.
 */

const REPO = 'LevelThreeLabsO/circuit-newswire';
const WORKFLOW = 'poll.yml';

function pingNewswire() {
  const token = PropertiesService.getScriptProperties().getProperty('GH_TOKEN');
  if (!token) {
    throw new Error('GH_TOKEN is not set in Script Properties.');
  }

  const url = 'https://api.github.com/repos/' + REPO +
              '/actions/workflows/' + WORKFLOW + '/dispatches';

  const response = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token.trim(),   // trim: a pasted newline reads as a revoked token
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    payload: JSON.stringify({ ref: 'main' }),
    muteHttpExceptions: true,
  });

  const code = response.getResponseCode();

  // 204 is success for this endpoint. Anything else throws, which turns the run red in
  // Executions and fires Google's own failure email to the trigger owner — the same way
  // the JI Newswire surfaces its failures. Never fire-and-forget.
  if (code !== 204) {
    throw new Error('Dispatch failed: HTTP ' + code + ' ' + response.getContentText().slice(0, 300));
  }
  console.log('dispatched 204');
}
