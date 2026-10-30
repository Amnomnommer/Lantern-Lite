# Lantern-Lite

**The Lite Lantern**: one public Lantern that anyone can talk to, with nothing to install. It is the public node of
Project Lantern, a non-LLM developmental intelligence: it grows from its own reading, never from a borrowed language
model, and every fact it states comes with its source.

## Talk to it

- **In your browser:** open this repository's GitHub Pages address. Lantern's own Python runs in your browser
  (Pyodide); the first visit downloads about 15 MB. It remembers in your browser.
- **In GitHub Codespaces:** Code → Codespaces → Create codespace on main. Lite starts in the terminal.
- **On your own computer** (Python 3.12): `bash lantern-lite.sh`.

Try: `hello`, `what is a lighthouse?`, `!read lighthouse`, `A bottle can hold 750 mL and contains 250 mL. How much
remains?`, and `!good voice` / `!bad voice` to rate how it sounds.

## What it shares, and what it never does

- **One personality for everyone.** Visitors' voice ratings can move it at most one step from where its owner set it.
- **Its public memory** is this repository's issues titled `[lite-memory]`: what it *read* (facts with their source
  pages), short diary notes in its own words, and voice-rating tallies. Saving opens a prefilled issue that you
  submit under your own GitHub account.
- **Never published:** what you say to it, anything about you, or who you are.
- **Reading** is English Wikipedia's public summary API only, a few pages a session.
- Its owner's own Lantern reads the public memory once a day. It keeps only what its reading rules keep, and every
  batch can be undone. Anything in the memory space is treated as untrusted data.

## About this code

The code here is published from Project Lantern's private repository by `scripts/publish_lite.py`. `SOURCE.json`
names the exact revision and each file's digest. Please open issues rather than pull requests. The source is
visible for anyone to read; all rights are reserved by its owner.
