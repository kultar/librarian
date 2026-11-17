# Registry format

Each API is one YAML file named `<id>.yaml` in the registry directory. Unknown fields are errors.

```yaml
id: jira                          # lowercase letters, digits, '-'; must match the file name
name: Jira Cloud
description: Issues, projects and users.
base_url: https://your-site.atlassian.net
status: verified                  # provisional | verified | blocked

auth:                             # one of:
  type: basic                     #   none
  secret_ref: jira/readonly       #   bearer   {secret_ref}
                                  #   api_key  {secret_ref, in: header|query, name}
                                  #   basic    {secret_ref}  secret holds "username:password"
                                  # and, on approved entries only (see the README):
                                  #   oauth2_user    each person's own sign-in
                                  #   oauth2_client  a token made from a client secret
                                  #   github_app     a token made per call from an App key
                                  #   aws_sigv4      each request signed; nothing stored

credential:                       # shown to the approver on the secure page
  create_url: https://id.atlassian.com/manage-profile/security/api-tokens
  scopes: [read:jira-work]
  instructions: Use a dedicated service account with browse-only permissions.
  validation_operation: getCurrentUser   # a cheap read used to test new keys

owners:                           # notification routing (optional)
  channel: "#atlassian-admins"
  approver_group: S0123ABCD       # Slack user-group ID → real @mention

operations:
  - id: getIssue
    method: GET
    path: /rest/api/3/issue/{issueIdOrKey}
    summary: Get an issue
    access: read                  # read | write | unknown
    parameters:
      - {name: issueIdOrKey, in: path}
      # - {name: path, in: path, allow_slash: true}   # a file path or git ref may contain "/"
      - {name: fields, in: query, description: Comma-separated fields}
  - id: searchPost
    method: POST
    path: /rest/api/3/search/jql
    access: read
    read_basis: reviewed          # required for non-GET reads; entry must be verified
    accepts_body: true
```

### Fields librarian sets when learning

```yaml
updated_at: 2026-11-16T09:00:00Z  # when the entry last changed
source:
  kind: docs                      # openapi | docs | inferred (model knowledge) | manual
  url: https://example.com/docs/api/
  pages: [https://example.com/docs/api/, https://example.com/docs/api/reference]
  fetched_at: 2026-11-01T08:00:00Z   # when it was learned from there
  content_hash: 3f1c...           # fingerprint used to notice changes
  checked_at: 2026-11-16T08:00:00Z   # when it was last compared with the source
  found_by: homepage              # given | well-known | homepage | search: how the source was found
  trust: official                 # official (the API's own domain) | docs-host | third-party
operations:
  - id: get_widgets_id
    origin: docs                  # spec | docs | model | manual
    evidence: "GET /v2/widgets/{id}"          # verbatim quote from evidence_url
    evidence_url: https://example.com/docs/api/
    confirmed_at: null            # set by a reviewer if wanted; runtime confirmations are stored
                                  # in the state database
    suspended: null               # a reason here refuses the operation until removed
    warning: null                 # shown to callers with every result
    response_shape: [id, name]    # field paths seen in a successful answer: the baseline for
                                  # noticing that the API's answers changed shape
  - id: createIssue
    accepts_body: true
    body:                         # what the body holds, read from the API's description
      fields:                     # (at most 40, the top of the body first)
        - {name: title, type: string, required: true, description: The title of the issue}
        - {name: "labels[]", type: string}            # a list (quote a name with brackets)
        - {name: state, type: string, values: [open, closed]}   # the values it takes
        - {name: assignee.login, type: string}        # a field inside another
        - {name: "tree[].path", type: string}         # a field of every item of a list
      more: 3                     # how many fields the description has beyond these
      encoding: form              # how the API takes it, and librarian sends it: json (the
                                  # default) | form | multipart (not sent: files)
      lists: brackets             # in a form, how a list of plain values is written:
                                  # repeated (tag=a&tag=b, the default) | brackets
                                  # (tag[0]=a&tag[1]=b) | commas (tag=a,b)
```

`body` is what agents are told an operation's body holds
([README](../README.md#big-apis)); nothing is checked against it. It is read from an OpenAPI
or Swagger description (not from documentation pages), and you can write it by hand. A field
inside another is `required` whenever that other one is given.

`encoding` is acted on: with `form`, the object an agent gives is sent as
`application/x-www-form-urlencoded`. A field inside another goes in brackets
(`metadata[order]=6735`), a list of objects is numbered (`items[0][price]=p1`), and a list of
plain values is written as `lists` says. An API whose description gives its nested fields the
style `deepObject` (Stripe) gets `brackets`.

## Acting as the person

`auth.type: oauth2_user` makes librarian the OAuth client for the API and each person sign in
once (see the README's "Acting as the person"). Fields: `authorization_url`, `token_url`
(both https), `scopes`, `client_id`, `client_secret_ref` (optional; a secret reference, never
under `librarian/`), `revocation_url` (optional; when set, disconnecting a person also revokes
their tokens there), `pkce` (default true), `extra_authorize_params`. Only verified entries may
use it. The same type works for `write_auth`.

## Credentials made for each call

Three kinds of `auth` (and `write_auth`) where nothing long-lived is sent to the API; the
README's "Credentials made for each call" says what each does. Only verified entries may use
them.

```yaml
auth:
  type: oauth2_client             # a token made from a client id and secret
  secret_ref: orders/client-secret   # the client secret; sent to token_url only
  token_url: https://login.example.com/oauth/token   # https
  client_id: librarian
  scopes: [orders.read]           # give reads and writes different scopes
  audience: https://orders.example.com   # optional: some providers want one
  client_auth: body               # body (default) | basic: how id and secret reach token_url
  extra_token_params: {}          # more form fields for the token request
```

```yaml
auth:
  type: github_app                # a token made per call from a GitHub App's key
  secret_ref: github/app-key      # the App's private key (PEM)
  app_id: "123456"                # the App id, or its client id
  installation_id: null           # optional: for operations that name no owner
  permissions: null               # optional: ask for exactly these ({contents: read}) instead
                                  # of what the installation has
  owner_param: owner              # the arguments that name the owner, the repository and the
  repo_param: repo                # organisation in this API's operations: the installation
  org_param: org                  # is found from them
```

```yaml
auth:
  type: aws_sigv4                 # every request is signed; nothing is stored
  service: execute-api            # the signing name: execute-api, s3, es, lambda, ...
  region: eu-west-1
  role_arn: null                  # optional: a role librarian assumes for this API
  external_id: null               # optional: the external id that role asks for
```

## Discovery and glossary

```yaml
discovery:                       # reads that teach instance-specific names
  - operation: getFields          # must be a read operation of this entry
    items_path: null              # where the list is (null = the response itself)
    key: id
    value: name
    key_prefix: customfield_      # keep only matching keys
glossary: {}                      # filled by discovery: {customfield_10020: Sprint, ...}
```

Operations may also carry `tags` (taken from the OpenAPI spec) to improve search.

## Writes

```yaml
status: verified                  # writes are never allowed on provisional entries
write_auth:                       # separate credential for writes (same shapes as `auth`)
  type: basic
  secret_ref: jira/write
operations:
  - id: addComment
    method: POST
    path: /rest/api/3/issue/{issueIdOrKey}/comment
    access: write
    writable: true                # opt in; every call still needs approval or a grant
    accepts_body: true
    rememberable: true            # default true; always false for DELETE
    # approval: auto              # writes run without a person (never DELETE), up to
    # auto_limit_per_day: 100     # this many a day; chosen in the console or when enabling
    parameters:
      - {name: issueIdOrKey, in: path}
```

A write runs after a person approves it on the approval page, under a grant (*approve and allow
similar writes*: pinned to the agent, the operation and the argument values the approver keeps
fixed, lasting as long as they choose), or automatically when an approver chose that for the
operation. A request may carry an `idempotency_key`; a repeat with the same key returns the
first result. Grants are adjusted, revoked or quieted on the console's Grants page.

## Pagination

Declare it on the entry (default for every operation) or per operation. Without it, `Link:
rel="next"` headers are followed. Callers ask for more pages with `max_pages` (1-20).

```yaml
pagination:                 # Jira search: send nextPageToken back until it disappears
  style: cursor
  cursor_param: nextPageToken
  next_cursor_path: nextPageToken
  items_path: issues        # pages are merged into one list here
```

| Style | Needs | Stops when |
|---|---|---|
| `link` | nothing | no `rel="next"` link |
| `cursor` | `cursor_param`, `next_cursor_path` | no cursor in the response |
| `offset` | `offset_param`, `items_path` (`total_path` optional) | empty page or offset ≥ total |
| `page` | `page_param`, `items_path` (`first_page`, default 1) | empty page |

## Field selection

Callers can pass `select` with dotted paths to keep only what they need. Lists are traversed
automatically: `["issues.key", "issues.fields.summary"]` returns just the key and summary of every
issue. This keeps large responses under the size cap and saves LLM context.

## Rules enforced by the policy engine

| Situation | Allowed? |
|---|---|
| `access: read`, GET/HEAD, provisional or verified entry | Yes |
| `access: read`, `read_basis: reviewed` (e.g. POST search), verified entry | Yes |
| Same, provisional entry | No |
| `access: write` or `unknown` | No |
| Entry `blocked` | No |
| Undeclared argument, missing required argument | No |
| Path argument containing `/`, `.` or `..` | No |
| Header argument named Authorization/Cookie/Host/X-Api-Key | No |
| Operation `suspended` (in the entry, or by change detection) | No |

## Lifecycle

Entries live in librarian's database, and so does every decision about them. A decision is in
effect on every replica the moment a person makes it.

1. **Onboard**: `librarian learn <id> --spec-url ...` (or the `onboard` A2A skill) saves a
   provisional entry and asks the approvers to review it (a Slack card, a console to-do).
   Search-like POSTs are listed in `notes` and on the review page.
2. **Review**: an approver checks base URL, auth and the operations that need a decision on
   the review page (or the API's page in the console), and approves or denies. Approving makes
   the entry `verified` at once; denying forgets the API. Nobody edits YAML.
3. **Decide later**: enabling writes, changing an operation's access, allowing slashes, the
   authentication: each is a click in the console or on an approval page, saved to the
   database and in effect at once.
4. **Block**: denying a credential request sets `status: blocked` so agents stop asking;
   unblocking returns the entry to what it was.
5. **Change**: re-checks (`drift.recheck_hours`) compare the entry with its source. Provisional
   entries follow the source; for verified entries librarian suspends risky operations and
   asks an approver to approve the update (see the README).
6. **Forget**: any API can be dropped from the console; the next agent that needs it teaches
   it again.

## The record (GitHub)

With `publish.type: github`, every approved entry and every later decision about it is also
committed to a repository: `<directory>/<id>.yaml`, one commit per decision, with what was
decided and by whom in the message. Forgetting an API deletes its file. The record exists so
the registry lives somewhere outside the database; **librarian never reads it back**, so
nothing edited or merged there changes what agents may do.

- The write happens after the decision, in the background. If GitHub refuses or is down the
  decision still stands; the console lists what is not written yet and why, librarian retries
  (after 2 minutes, then 4, 8, ... up to an hour), and the API's page offers *Write it now*.
- librarian commits to the base branch. If a rule there only allows pull requests, it opens
  one from a `librarian/record-<id>-...` branch and merges it as the App in the same step, so
  `librarian/*` branches must stay unprotected and the App must be allowed to merge (no
  required review for it: add the App to the rule's bypass list). A pull request a rule kept
  from merging is reused for the next decision, so there is at most one per API.
- Provisional (unapproved) APIs are not written. Neither is the glossary, which holds names
  read from the API with its credential.
- **Restoring.** `librarian registry restore` brings the record's entries into the database:
  after losing the database, or when moving to a new one. Entries the database already has are
  kept unless you pass `--replace`. This is the only time the record is read.

## Registry files

`registry_dir` holds entries written by hand, in the format on this page.

- **Without a database** (`database_url: null`) the files are the registry; librarian saves its
  own changes there too (or into `registry_overlay_dir`).
- **With a database** (the default) files are imported into it. A new file is imported at start
  and whenever it appears. An edited file is imported again as long as nobody has decided
  anything about that API in librarian (glossary updates do not count); once someone has, the
  database's entry is used, the console says the file is being ignored, and forgetting the API
  imports the file afresh. Deleting a file does not remove the API: forget it in the console.

A file's `status` is taken as written, so whoever can change those files can add a verified
entry: treat the directory like the config file. Keeping hand-written entries in a git
repository pulled by git-sync (the chart's `registry.gitSync`) still works; do not point it at
the record's directory, which librarian writes itself.

## Upgrading from 0.19 or earlier

Before 0.20 the reviewed registry was a git repository: approving meant merging a pull request,
and git-sync brought the result back. On the first start of 0.20:

- every entry in the registry directory is imported into the database, and librarian's own
  earlier changes (blocks, glossaries) are kept exactly as 0.19 applied them;
- review pull requests that librarian left open are closed with a comment saying where the
  review continues: learned APIs still wait under *Waiting* in the console, and write requests
  that were enabled but never merged show up on the API's page with an *Apply* button;
- the repository keeps receiving every decision, now as commits.

So upgrade with git-sync still on, check the console lists your APIs, and then switch
`registry.gitSync` off (set `githubApp.repo` to the repository). If the files were already gone
when 0.20 first started, run `librarian registry restore` once.
