"""hone-node — an AI review node for hone.

A from-scratch containerized worker: it claims prepare / review / train /
draft tasks from hone-core over the v1 REST API, does the AI work
locally (the Claude API token never leaves the node), and reports the
result back. See ../docs/ARCHITECTURE.md and ../docs/API.md.
"""
