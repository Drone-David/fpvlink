# Contributing to FPVLink

Contributions are welcome — bug reports, hardware findings, protocol work,
patches. A few things are worth stating up front, because this project's licence
is unusual and the honest version is better than a surprise later.

## The licensing situation, plainly

FPVLink is under the [PolyForm Noncommercial License 1.0.0](LICENSE). It is
source-available, not open source: anyone may use and modify it for
noncommercial purposes, and **commercial rights are reserved to the copyright
holder**, who sells assembled units.

That creates an asymmetry you should understand before you spend an evening on a
patch. If you contribute code and nothing else is agreed, you hold copyright on
your lines. That would leave the project unable to sell units containing your
work without your permission — and it would mean your contribution is being
distributed under a licence that lets one party monetize it and not you.

## Contributor terms

By submitting a contribution (a pull request, patch, or code in an issue), you
confirm that:

1. **It is yours to give.** You wrote it, or you have the right to submit it, and
   it is not encumbered by an employer agreement or another project's licence.
   If it derives from someone else's code, say so and name the licence — this
   matters more than usual here, because most permissive licences allow reuse in
   a noncommercial project but a copyleft one does not.

2. **You grant a broad licence to the project.** You give David Escobar a
   perpetual, worldwide, non-exclusive, royalty-free, irrevocable licence to
   use, reproduce, modify, distribute and sublicense your contribution, and to
   relicense it — including under commercial terms and as part of units sold
   commercially.

3. **You keep your copyright.** This is a licence grant, not an assignment. Your
   work stays yours; you can use it elsewhere however you like.

If you are not comfortable with point 2, please open an issue to discuss before
writing code rather than after. A patch that cannot be shipped commercially is
one the project probably cannot merge, and it is better to find that out early.

> This is a plain-language contributor agreement written for a small project, not
> reviewed by a lawyer. If FPVLink grows a real contributor base or you are
> contributing on behalf of a company, it is worth replacing this with a proper
> CLA.

## Practical notes

- **Hardware claims need hardware.** This project's history is full of fixes that
  looked right and were not. If you change the capture path, the pipeline, or
  anything touching latency, say what you tested it on and what you measured.
- **The dashboard has no authentication.** See the Security section of the
  README. Do not add features that widen that surface without addressing it.
- **Run the checks** before opening a PR:
  ```
  npm test && npm run lint && python3 -m py_compile $(git ls-files '*.py')
  ```
- **Do not commit third-party firmware, filesystem dumps, or vendor binaries.**
  `scratch/` is gitignored for exactly this reason. Reference material stays on
  your machine.
