# Security Policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it through [GitHub's private vulnerability reporting](https://github.com/showjihyun/livingfeed/security/advisories/new), which opens a private advisory visible only to the maintainers.

Include what you need to make the problem reproducible: affected component, steps, and what an attacker gains. A proof of concept helps but is not required to file.

You can expect an acknowledgement within a week. If a report leads to a fix, you will be credited in the advisory unless you would rather not be.

## Scope

Living Feed currently runs locally — there is no hosted deployment, so there is no production system to attack. What is in scope is the code in this repository: the services in `services/`, the simulation in `engine/`, the web client in `apps/web`, and the local stack definition in `infra/`.

Two things worth knowing before you file:

- **The local stack ships with development credentials on purpose.** `livingfeed:livingfeed`, open ports, and no auth on the data stores are intended for a single-machine development environment, not a finding. When a hosted deployment exists, its configuration will be separate.
- **Character output is model-generated.** Posts, replies, and DMs come from an LLM. Prompt injection that changes what a character says is expected behavior in a simulated social world. What is a finding: injection that escapes the character into the host system — reading files, reaching the event store, executing code, or crossing into another player's private data such as the Hidden Feed or inbox.

## Handling player data

The world stores what players write and what characters remember about them. If you find a path that exposes one player's private timeline, messages, or memories to another, treat it as a vulnerability and report it privately.
