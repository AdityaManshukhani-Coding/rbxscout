# Browser-login / OAuth2 plan notes

Short answer:
- Not just “log in and keep the tab open.”
- If you go the OAuth2 route, the typical flow is:
  1. open a browser to Discord’s authorization page
  2. the user completes login + consent in that browser
  3. Discord redirects to a local callback with an authorization code
  4. your backend exchanges that code for an access token
  5. you use the resulting token according to what is actually allowed

What the browser login does
- it lets the user sign in and consent to your application
- the resulting access token is what is used for later API calls
- the browser session is separate from the token lifetime

What it does not magically do
- it does not make a user token behave like a bot token for joining guilds
- it does not bypass bans, verification levels, membership screening, member limits, or missing permissions
- it does not guarantee the account ends up in the target guild unless the guilds.join prerequisites are met

What you would need for guilds.join specifically
- a Discord application
- that application’s bot already in the target guild
- the bot to have CREATE_INSTANT_INVITE there
- the user to authorize with guilds.join
- the user’s access token sent in the Add Guild Member request, authenticated with the application’s bot token

Practical setup points
- register the exact redirect URI in the Discord Developer Portal, including scheme, host, port, and path
- localhost HTTP is common for local development; HTTPS is the normal production pattern
- generate a fresh state value per login attempt and verify it on callback
- exchange the code server-side, not from browser JavaScript
- treat the authorization code as short-lived and one-use
- store tokens securely, not in plain text in the workspace

Relevant lifetimes
- browser login session: separate from OAuth tokens; do not assume a fixed duration
- access token: has an expires_in value in the token response
- refresh token: can be exchanged for a new access token, but may still fail or be revoked

So for this goal
- if the real problem is “the account cannot join manually,” the OAuth2 web-login path can help only if the prerequisites for whatever you are trying to do are met
- if you only need the account to be in the guild so you can run owner/highest-admin/highest-top-role discovery, then the prerequisite is still “the account is actually in the guild,” not just “we got an OAuth token”
