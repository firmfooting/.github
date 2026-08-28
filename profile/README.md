<p align="center">
  <img alt="firmfooting" src="https://raw.githubusercontent.com/firmfooting/branding/main/assets/logo.png" width="240">
</p>

# firmfooting

Safe, plain tooling for M365 & SharePoint operators — firm footing for doing it yourself.

`firmfooting` tools let site owners and operators provision and manage their own SharePoint without tenant admin, app registration, a premium licence, or installed tooling. You paste a script into the browser console; the tool does the rest. No magic, no silent surprises — every tool measures first and refuses to assume.

## Repositories

| Repo | What it is |
|---|---|
| **[dbml-sharepoint](https://github.com/firmfooting/dbml-sharepoint)** | Turn a DBML schema plus a mapping into browser-paste scripts that provision SharePoint lists, views, validation, and permissions. |
| **[branding](https://github.com/firmfooting/branding)** | The mark, colour system, and brand guidelines. |

## What we care about

- **Measure, never assume.** Nothing ships on plausibility. Every behaviour is probed against a live tenant first, and the measured fact is recorded, not guessed.
- **Fail closed.** A guard that refuses is better than a rule that silently does nothing. If the tool can't be sure, it stops.
- **Plain and inspectable.** Plain words, plain files, SQL you can read, scripts you can read before you paste. Clever is a cost, not a feature.
- **Honest evidence.** Findings are captured as hashed screenshots and accessibility snapshots, with a chain from probe run to verdict. The run documents itself.

<p align="center"><sub>firmfooting is independent tooling. Not affiliated with or endorsed by Microsoft.</sub></p>
