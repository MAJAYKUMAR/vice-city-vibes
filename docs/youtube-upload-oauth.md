# YouTube upload OAuth setup (for `scripts/post_queue.py --live`)

`post_queue.py` uploads through the **official YouTube Data API v3**.
Dry-run mode needs no setup at all. `--live` needs the steps below,
done once by Ajay. **No credentials are in this repo and none should
ever be committed to it.**

## What you need

- The Google account that owns the Vice City Vibes YouTube channel.
- About 15 minutes, in a browser, signed in to that Google account.

## Steps

1. **Create a Google Cloud project.** Go to Google Cloud Console
   (console.cloud.google.com), create a new project (any name, e.g.
   `vice-city-vibes`), and select it.
2. **Enable the YouTube Data API v3.** In the console, open
   "APIs & Services" → "Library", search for "YouTube Data API v3",
   open it and click Enable.
3. **Configure the OAuth consent screen.** In "APIs & Services" →
   "OAuth consent screen": choose user type External, fill in the app
   name and your contact email, and save. (Look for the consent-screen
   section in the left menu — Google renames these screens
   occasionally, so follow the labels you see.)
   - While the app is in **Testing** mode, only Google accounts you add
     as test users can sign in, and tokens expire after about 7 days.
     Add your channel's Google account as a test user. Moving the app
     to Production avoids the 7-day expiry; Google may ask for app
     verification first for this scope — follow its prompts.
4. **Create an OAuth client (Desktop app).** In "APIs & Services" →
   "Credentials" → "Create Credentials" → "OAuth client ID". For
   application type, choose **Desktop app**. Name it anything
   (e.g. `vice-city-vibes-uploader`).
5. **Download the client secret.** On the credentials page, download
   the JSON for the client you just made. Save it as
   `~/.config/vice-city-vibes/client_secret.json` — **outside the repo**,
   never under the project folder. Do not rename it to anything else
   and do not share it.
6. **Install the Google API libraries** (one time, on the machine that
   runs uploads):
   `pip install google-api-python-client google-auth-oauthlib google-auth-httplib2`
7. **First run — consent flow.** Run:
   `python3 scripts/post_queue.py --live --id <one-test-entry-id>`
   A browser window opens: sign in to the channel's Google account,
   accept the consent screen, and pick the right channel if asked.
   The script then saves a token to
   `~/.config/vice-city-vibes/token.json` (also outside the repo).
   Later runs reuse that token and refresh it automatically — no
   browser needed unless the token is revoked or expires in Testing
   mode (see step 3).

## Scope

The script requests only one scope: `youtube.upload`
(`https://www.googleapis.com/auth/youtube.upload`) — upload videos
to the channel. It cannot read or change anything else on the account.

## Verify it works

1. `python3 scripts/post_queue.py --dry-run` — prints what would be
   uploaded, no network, no credentials needed.
2. `--live` on one test entry (step 7 above), with `--privacy private`
   (the default) so the video is not public until you check it.
3. Open YouTube Studio, confirm the video is there, then confirm the
   **"Altered content"** setting for it — `videos.insert` has no field
   for that disclosure, so it must be checked in Studio. The
   AI-disclosure line is also included in the video description.
4. Only then re-run `--live` (optionally with `--privacy public`)
   for the rest of the queue.

## If something goes wrong

- `--live` with no `client_secret.json` refuses cleanly and tells you
  the missing path — run the steps above.
- "Access blocked / unverified app" at consent: the app is in Testing
  mode and the signing-in account is not a test user — add it (step 3)
  or move the app to Production.
- Revoked or expired token: delete `~/.config/vice-city-vibes/token.json`
  and re-run — the consent flow starts again.
- Never paste `client_secret.json` or `token.json` contents into chat,
  email, or any repo. If one ever leaks, revoke it in the Cloud Console
  (Credentials page) and re-do steps 4–7.
