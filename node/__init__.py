"""hone-node — an AI review node for hone.

A from-scratch containerized worker: it claims review and maintenance tasks
from hone-core over the v1 REST API, does the AI work locally (the Claude
API token never leaves the node), and reports the result back. See
../ARCHITECTURE.md and ../API.md.
"""
