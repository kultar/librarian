---
name: librarian
description: Use web APIs through librarian's tools (capabilities, execute, fetch, write, write_plan, write_status, enable_status, onboard) instead of calling them yourself. Use when a task needs data from an HTTP API or a change in one (Jira, GitHub, an internal service); when a librarian tool answers pending_approval, credential_pending, consent_pending, write_not_enabled or too_large; or when an API that librarian does not know yet has to be added.
license: Apache-2.0
---

# Using APIs through librarian

librarian holds the credentials and the rules. You never see a key and never call an API
yourself: you ask librarian, it checks its policy, makes the call, and gives you the answer
with where it came from. What it refuses, you cannot get another way, and should not try to.

## Reading

1. `capabilities` shows the APIs you may use and their read operations. Give it a `query` in
   a few words ("jira issue comments") to get the best matches first. Each operation names
   the arguments it takes in `args` (`team?` is one you may leave out) and says when it takes
   a `body`: use those names exactly, do not guess them.
   - With `api` (an API's id) it shows that API alone; add a `query` to get its operations
     that fit what you need. Use this once you know which API you want.
   - `total_read_operations` and `total_write_operations` say how many operations an API
     has. When its lists hold fewer, the answer was shortened to stay small and says so in
     `note`: do what the note says (`api`, and a `query`) instead of guessing an operation
     that is not listed. APIs named only in `more_apis` can be asked about the same way.
2. `execute` runs one read: `api`, `operation`, `args`.
   - Ask only for what you need: `select` takes dotted paths, and lists are followed, so
     `["issues.key", "issues.fields.summary"]` returns those two fields of every issue.
   - `status: "too_large"` means the answer was not sent because it would not have fitted.
     `outline` shows its fields and how many items its lists have. Call `execute` again with
     `select` for the fields you need, or with arguments that narrow the request.
   - More than one page: `max_pages` (up to 20).
3. `fetch`, where you have it, answers a request in plain language by making the reads itself.
   It is slower than `execute`; use it when you do not know which operations to call.

Every answer carries `provenance`: which API and operation, the address, when. Say where a
fact came from when it matters.

## Changing something

- Which operation makes a change is in `capabilities` too: where you may ask for changes, an
  API lists its `write_operations`. `enabled: false` means nobody has allowed writes through
  that operation yet (see `write_not_enabled` below).
- `write` asks for one change: `api`, `operation`, `args`, `body`, and a `reason`. A person
  reads the reason before approving, so say in one sentence what you are doing and why.
- Several writes that belong together (a branch, a commit, a pull request) go in one
  `write_plan`: the person sees all of them and approves once, and librarian makes them in
  order. Where a later step needs what an earlier one returned, write
  `{"$from": "<step id>.<field>"}` as the value. Read what you need with `execute` first:
  only writes go in a plan. The first step that fails ends the plan and nothing is undone, so
  order the steps so that half a plan does no harm.
- `status: "executed"`: it is done. `how` says why it did not wait (`grant`: a person allowed
  writes like this before).
- `status: "pending_approval"`: a person has been asked. Call `write_status` once with the
  `approval` id and `wait_seconds` (45 is a good value): it answers as soon as the write has
  been made or refused. Still `pending` after that? Tell the user that it waits for approval,
  and that you can look again when they ask. Do not send the write again: the same request is
  already waiting, and a changed one would be a second request.
- `status: "write_not_enabled"`: nobody has allowed this operation for writing yet, and the
  approvers were asked. `enable_status` with the `enable_request` id says where that stands.
  When your task will need other operations of the same API too, name them in `also_needs`.
- For a plan, `write_status` lists every step: `made`, `failed`, `not sent`, or `unknown`.
  `unknown` means the step may or may not have reached the API: read the state with
  `execute` before you send anything again.
- Something you may have to repeat (a retry after an error) gets an `idempotency_key` of your
  choosing: the same key never makes the same change twice.

## When librarian has to ask a person

- `credential_pending`: librarian has no key for that API yet, and the people who can add one
  were told. Say so, and try again later. Never ask the user for a key or a token, and never
  put one into `args` or `body`: credentials do not go through you.
- `consent_pending`: that API acts as the person you work for, and they have been sent a link
  to sign in. Tell them, and try again after they have.
- "refused: ..." is a decision, with the reason in words. Pass the reason on as it is. Do not
  look for another operation, another API or another tool that would do the same thing.

## An API librarian does not know yet

`onboard` teaches it one: an `api` id of your choosing, and `spec_url` (an OpenAPI document),
or `base_url` (librarian looks for the description there), or `docs_url` together with
`base_url` when the API has only documentation pages. It can be read from at once, and a
person is asked to review it. Writes on it need that review first.
