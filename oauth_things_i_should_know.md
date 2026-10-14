# OAuth2 in plain language

Short version:
- OAuth2 is not “give us one code and then we are done forever.”
- It is “the user approves your app once, and your app gets keys it can use later.”

Two kinds of keys matter here:
1. access token
   - this is what you actually use to call Discord APIs
   - it expires
2. refresh token
   - this is what lets your app get a new access token without asking the user to approve again
   - it can still stop working if the user revokes access or something else invalidates it

So the practical answer is:
- you do not usually have to make the user approve every time your bot runs
- but you do need to store the refresh token securely so your app can keep getting new access tokens
- and you still have to handle the case where refresh stops working and the user must reconnect

What OAuth2 does not do
- it does not make a user token act like a bot token
- it does not magically put the account in a guild unless the guild-specific prerequisites are met
- it is not a permanent guarantee; it lasts until it is revoked or becomes invalid

What “approve once” actually means
- the user authorizes your application with the scopes you asked for
- after that, your app can keep using the refresh flow for that authorization
- if you need the user to approve again, you ask for it on purpose, not because tokens normally expire every run

What you would store for repeated use
- the refresh token
- the client secret on the backend, not in front-end or shared code
- the access token expiry so you can refresh before it dies
- ideally keep all of this on a server you control, over HTTPS, and out of logs, URLs, and error reports

Why this matters for your goal
- if your goal were only “run as the authorizing user through supported endpoints,” then OAuth2 can be a long-term integration
- if your goal is “get the account into the guild so we can discover owner/highest admin/highest top role and DM them,” then the real requirement is still that the account is actually in the guild, not just that we have a token

One-time vs every time
- authorization is one-time per grant, plus refresh over time
- using the token happens every time you need to do something
- re-authorization happens only when refresh fails or you deliberately ask again

Security note
- a refresh token is powerful
- if someone gets it, they can often keep getting access tokens
- so treat it like a secret, not like a disposable code
