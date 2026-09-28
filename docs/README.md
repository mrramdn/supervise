# supervise documentation

supervise is a Claude Code plugin that runs a coding task through plan, implement, verify, and review, with the
rules checked by a small CLI instead of by the prompt. These pages explain how to install it, how a run works, how
to configure it for your project, and what to do when the CLI refuses a step.

| Page | Read it when |
|---|---|
| [Getting started](getting-started.md) | You want to install supervise and run your first task. |
| [Concepts](concepts.md) | You want to know what the phases, gates, fingerprint, and review rules mean. |
| [Configuration](configuration.md) | You want to check or change the gates, the fix-round cap, or what blocks a review. |
| [Troubleshooting](troubleshooting.md) | The CLI refused a step, or a run did something you did not expect. |
| [CLI reference](../skills/supervise/references/cli.md) | You need every command, exit code, and file format. |

For the reasons behind each rule, see [DESIGN.md](../DESIGN.md). For what changed between versions, see
[CHANGELOG.md](../CHANGELOG.md). The results of the Claude Code experiments the design is based on are in
[spike-findings.md](spike-findings.md).
