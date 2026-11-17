# Security Policy

## Supported versions

There are no releases yet. Only the current `main` branch receives fixes, so update
your checkout before reporting.

## What counts

sch2kicad runs entirely on your own machine. It has no accounts, makes no network
calls and has no telemetry. The interesting surface is:

- **Files the tool reads.** PDFs and images from other people go through pdfium,
  Pillow and OpenCV, and `circuit.json` and `board.json` are read as written.
  Anything a crafted file can do beyond a crash or a wrong schematic is in scope.
- **Files the tool writes.** It should only write to the work directory beside the
  input and to the output directory you name. Anything that makes it write or
  overwrite somewhere else is in scope.
- **Programs the tool runs.** It runs `kicad-cli`, and `compare --open` opens the
  image it made. Anything that makes it run something else, or pass a file's
  contents to a shell, is in scope.
- **Other people's text reaching Claude.** With the skill, the text of a build
  document (its notes, its parts list, the words on the drawing) is read by Claude.
  Text in a document that gets Claude to do something other than convert the
  schematic is in scope.
- **Dependencies** pulled in through Python packages.

A crash on a malformed file, or a schematic read wrongly, is an ordinary bug unless
it leads to something more. Vulnerabilities in Claude Code itself go to
[Anthropic](https://www.anthropic.com/responsible-disclosure-policy), and those in
KiCad to the [KiCad project](https://www.kicad.org/).

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private
reporting instead: **Security → Report a vulnerability** on the repository, or
https://github.com/drlholloway/sch2kicad/security/advisories/new

Include the commit you are on, your platform and KiCad version, what you observed
and how to reproduce it. A sample file is ideal. You will get an acknowledgment
within a week. Fixes land on `main` with a credit in the commit unless you prefer to
stay anonymous.

## Dependencies

Dependabot watches the Python dependencies weekly, and CodeQL scans the code on
every push.
