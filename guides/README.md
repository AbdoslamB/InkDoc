# Developer guides

One guide per area of InkDoc's code, for contributors and coding agents.
[AGENTS.md](../AGENTS.md) is always read. It holds what every change needs and a table that
routes each file to its guide. A guide holds what only changes in its own area need, and it
is read before working there. That keeps the always-read part short as InkDoc grows, while
nothing about an area is more than one file away.

| Guide | Area |
|---|---|
| [conversion.md](conversion.md) | Conversion core, the MarkItDown worker, saving |
| [auto-quality.md](auto-quality.md) | The Auto engine and the "Auto quality" steps |
| [tables.md](tables.md) | The table stage, the table finder and check, tables on scanned pages |
| [engines.md](engines.md) | Docling pack, add-ons, GLM-OCR |
| [server.md](server.md) | The REST API, settings, URL conversion |
| [ui.md](ui.md) | The shared web and desktop UI |
| [desktop.md](desktop.md) | The desktop window and its bridge to the page |
| [release.md](release.md) | CI, release builds, updates, the website |

## What a guide contains

Every guide has the same four sections, in this order:

1. **Rules.** What must stay true in this area. Breaking one is a bug, even if the tests pass.
2. **How it works.** The design, module by module, with the reasons behind it.
3. **Tests.** Which tests cover the area, so a change knows what to run and extend.
4. **Lessons.** What earlier work in this area taught. See below.

A rule that holds across areas belongs in AGENTS.md ("Rules that apply everywhere"), and the
guide's own copy is replaced by a pointer to it. Each fact lives in one place.

## Lessons

A lesson records something a later change in this area would otherwise get wrong. It has
three parts:

```markdown
### Short title saying what to do
- **When:** the situation in which this applies (the cue that should bring it to mind).
- **Lesson:** what to do or avoid, and why.
- **Evidence:** the commit, test or reproducible check that showed it.
```

**Who adds it.** A person. An agent drafts lessons and proposes them, with their evidence;
it never writes one into a guide on its own. Drafts can be kept until someone reviews them, but
only a person moves one into a guide.

Propose a lesson only when all of these are true:

- **There is evidence.** A commit, a test, a failing CI run or a measurement you can name. A
  suspicion is an issue to open, not a lesson.
- **It will come up again.** It applies to future changes, not only to the one that taught it.
  What a change did belongs in its commit message and in `CHANGELOG.md`.
- **It is in the right guide.** Put it in the guide for the area where the cue appears, so the
  lesson is read by whoever works there next.
- **A test can't do the job.** If the rule can be checked mechanically, write the test. The
  lesson can then be one line citing it, or be left out.

When a lesson turns out to be wrong or out of date, fix it or delete it in the same change that
shows it. A wrong lesson does more harm than a missing one.

## Changing the guidance

AGENTS.md, these guides, and any agent skills or reviewer agents are tools, and they get the
same care as code. A change to them follows three steps:

1. **Propose.** Name the problem a real task ran into: a mistake made, a question asked twice, a
   rule missed. Then name the smallest change to the guidance that would have prevented it.
2. **Probe.** Try the change against that task, or a similar one, and compare the result with
   what happened before. Guidance that sounds right but doesn't change behaviour is only cost.
3. **Compose.** A person decides what to keep. Agents may run the first two steps and draft the
   result, but adopting a change to AGENTS.md or a guide is never automatic. Keep what helped,
   and put it where it costs least:

| Where | Read | Holds |
|---|---|---|
| AGENTS.md | on every task | the contract, commands, rules that apply everywhere, the routing table |
| a guide | before work in its area | how the area works, its rules, its lessons |
| a test | on every CI run | any rule that can be checked mechanically |
| a script or skill | when run | a procedure with fixed steps |

Prefer the lower rows: a test enforces a rule without anyone having to read it, and a guide
costs nothing on tasks that don't touch its area. Move something into AGENTS.md only when
missing it would cost more than reading it on every task.

## What keeps the guides correct

`tests/test_agent_guides.py` runs in CI and fails when:

- a file under `app/`, or `main.py`, is covered by no guide in AGENTS.md's table;
- a route in that table, or a repository path a guide names, no longer exists;
- a guide is missing from the table, so no one would ever be sent to it;
- a link or `#anchor` between these files is broken;
- a lesson lacks its When, Lesson or Evidence;
- the Node UI tests named in AGENTS.md, the ones in `tests/` and the ones CI runs differ.

The test checks structure, not truth. A guide that describes code which has since changed is
caught only by whoever changes that code: the guide update belongs in the same change. An agent
proposes that update, and makes it once a person approves.
