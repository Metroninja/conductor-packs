# calendar-busy-sync

Keep several Google calendars honest about when you are busy.

If you have a work calendar, a second work calendar and a personal one, people
booking you on one cannot see what the others hold. This pack copies busy time
across: every event on a **source** calendar becomes a private "Busy" block on
every other **target** calendar. Blocks follow the source event — moved when it
moves, removed when it is cancelled, declined or marked free.

Pure verbs and two small js steps. No agent, no model, no cost.

## What it does

One workflow, `reconcile`, shared by two triggers:

| Trigger | Fires on | Why |
|---|---|---|
| `on_change` | a source calendar changing (Google push, via the connector's `changed` source) | blocks appear seconds after an edit |
| `sweep` | your `busy_sync` cron schedule | Google push is best-effort and nothing arrives while the daemon is down; the sweep also rolls the window forward |

Each run reads every source and target for the next `window_days`, works out
which blocks should exist, and creates, moves or removes blocks until the
targets match. It keeps no state of its own: a block carries a private extended
property naming the source event it stands for, so a second run is a no-op and
a missed run is caught up by the next one.

What counts as busy: an event that shows as busy and that you have not
declined. Skipped: events marked free, declined invitations, working-location
entries, and all-day events (unless `mirror_all_day` is on).

It does not double up. A calendar never gets a block for its own events, a
block is never copied onward, and a meeting you are invited to on two accounts
gets no block on either, since both calendars already hold it.

## Requires

- conductor `>=0.106.0` (list sockets).
- The [`google-calendar`](https://github.com/NodeSpy/conductor-plugins/blob/main/docs/connectors/google-calendar.md)
  connector `>=1.2.0`, one instance per calendar.
- A `cron` connector with a `busy_sync` schedule.
- For `on_change`: an exposure connector (smee, tailscale funnel, cloudflared,
  ngrok) so Google can reach each source connector's `watch` receiver. Without
  one, arm only `sweep`.

## Google setup

One Google Cloud project and one OAuth client serve every account, work and
personal alike. Each calendar then logs in on its own.

### 1. Create the OAuth client

1. In the [Google Cloud console](https://console.cloud.google.com), create a
   project. A personal Google account is the simplest owner: a project inside a
   Workspace organisation can be limited to that organisation's users.
2. **APIs & Services → Library** → enable the **Google Calendar API**.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID.**
   The console first asks for the consent-screen basics: an **app name**, a
   **support email**, audience **External**, and a contact email.
4. Application type **Web application**. Under **Authorized redirect URIs** add
   `http://localhost:8400/callback`. Leave JavaScript origins empty.
5. Put the client id and secret in conductor's environment
   (`conductor.env`) as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

That is enough to log in and try the pack. The app is now in **Testing**.

### 2. If you stop there: Testing, with only an app name

It works, with two catches:

- Only accounts listed under **Audience → Test users** can log in. Add each
  calendar's account.
- **Google expires every refresh token after 7 days.** The sweep then fails
  until you run `conductor connector auth <name>` again for each calendar,
  every week.

Fine for a trial. For anything you want to leave running, publish.

### 3. Publish: branding, then audience

Publishing does not mean submitting for Google's verification, and nobody else
gains access. It only lifts the 7-day expiry.

1. **Branding.** Google will not publish without an **application home page**,
   and a home page needs its domain listed under **Authorized domains**. Use a
   domain you control: home page `https://example.com`, authorized domain
   `example.com`. Ownership is only checked if you submit for verification.
   Leave the **logo** empty — uploading one makes Google ask for verification.
2. **Audience → Publishing status → Publish app**, and confirm.

Google will say the app needs verification because it asks for calendar
access. You can leave it unverified: the login shows a "Google hasn't verified
this app" page (**Advanced → Go to …**), and the app is capped at 100 users.

**Log in again after publishing.** A token issued while the app was in Testing
keeps its 7-day expiry; only a login made after publishing gets one that lasts.

### 4. Work accounts

A Google Workspace admin can block unverified third-party apps. If a work
account refuses the login, its admin has to trust your client id: Admin console
→ **Security → API controls → App access control**.

### 5. Log in each calendar

With the connectors from [Install](#install) in your config, log in once per
connector, choosing the matching Google account in the browser:

```sh
conductor connector auth cal-personal
conductor connector auth cal-work
conductor connector auth cal-side
```

`conductor connector auth ls` shows each one as logged in. The access token it
lists lasts an hour; conductor renews it from the stored refresh token, so
there is nothing to redo unless that refresh token expires (step 2) or you
revoke access.

## Install

```yaml
vaults:
  calsync: { type: conductor }          # holds the three refresh tokens

connectors:
  timer: { use: cron, schedules: { busy_sync: { every: 15m } } }

  cal-personal:
    use: google-calendar
    auth: &gauth
      grant: authorization_code
      client_id: ${GOOGLE_CLIENT_ID}
      client_secret: ${GOOGLE_CLIENT_SECRET}
      token_vault: calsync
  cal-work:
    use: google-calendar
    auth: *gauth
  cal-side:
    use: google-calendar
    auth: *gauth

packs:
  busy:
    source: github.com/NodeSpy/conductor-packs//calendar-busy-sync
    version: "=0.1.0"
    connectors:
      sources: [cal-personal, cal-work, cal-side]   # whose events block time
      targets: [cal-work, cal-side]                 # where blocks are written
      cron: timer
    settings:
      dry_run: true                                 # first run: log the plan only
    triggers:
      sweep: { enabled: true }
```

Then `conductor vault calsync init`, `conductor init`, `conductor pack plan`,
`conductor validate`.

That is sweep-only: a block appears within one `busy_sync` interval of the
event. To get blocks within seconds, give each **source** connector a `watch:`
block and arm `on_change` as well:

```yaml
  cal-personal:
    use: google-calendar
    auth: *gauth
    watch: { listen: 127.0.0.1:8411, expose: cal-relay }   # an exposure connector of its own
```

```yaml
    triggers:
      on_change: { enabled: true }
      sweep:     { enabled: true }   # keep it: push is best-effort
```

Give each calendar its own exposure. A smee channel shared with another
listener (your GitHub webhooks, a second calendar) delivers every message to
all of them. The sweep path is the one this pack has been run on; the push
path is wired but not yet exercised.

Leave a calendar out of `targets` to keep blocks off it. Nobody books you
through your personal calendar, so the example above reads it but never writes
to it — which also means a work meeting shows twice in a merged view, not three
times.

Start with `dry_run: true` and read the plan line in the daemon log:

```
calendar-busy-sync: 20 to create, 0 to move, 0 to remove across 2 target(s) from 3 source(s); blocks wanted: cal-personal -> cal-side 10, cal-personal -> cal-work 10
```

When the split looks right, remove `dry_run` and reload.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `window_days` | `30` | How far ahead to mirror |
| `block_title` | `Busy` | Title of a block |
| `show_details` | `false` | Use the source event's title instead. Blocks stay private either way |
| `color_id` | `8` | Google event colour for blocks (1–11; 8 is graphite). Empty keeps the calendar's colour |
| `mirror_all_day` | `false` | Also mirror all-day events that are marked busy |
| `tag` | `conductorBusySync` | Extended-property name marking a block as this pack's |
| `max_deletes` | `50` | A run that would delete more blocks than this fails instead |
| `batch_size` | `50` | Most blocks created, moved or removed per run. The rest follow on the next runs. Keep it under your `policy.max_fan_out` (100 by default) |
| `dry_run` | `false` | Plan and log only |

## Good to know

- **Blocks are real events on the target's calendar.** That is what makes other
  people's scheduling see them, and it is why a calendar app showing all your
  accounts at once shows the event and its blocks side by side. A muted
  `color_id` keeps them in the background.
- **Blocks are private and silent**: visibility `private`, no reminders, no
  attendees, so nobody is notified.
- **Removing a calendar from `sources` removes its blocks** on the next run.
  Uninstalling the pack does not; the blocks it already wrote stay until you
  delete them.
- **Renaming a connector re-creates its blocks**, since a block is keyed by the
  source connector's name.
- **Two runs at once can create a duplicate block.** The next run removes it.
- **The triggers lift conductor's attempt limit.** conductor backs off and
  then parks a trigger that keeps firing for one target (`max_attempts_per_head`),
  which would stop a sweep after six runs. The pack's triggers set that limit
  out of reach and run one at a time.
- **A big first sync takes a few runs.** Each run writes at most `batch_size`
  blocks, so a calendar needing 130 fills in over three sweeps.
- **Past blocks are left in place.** Only the window ahead is reconciled.

## Tests

```sh
python3 -m unittest discover -s calendar-busy-sync/tests   # needs node + PyYAML
```

The tests run the manifest's `plan` and `window` js steps under node. The
workflow's Google calls are not exercised.
