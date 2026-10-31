# Kilo registration live smoke — 2026-10-14

Live launch evidence from the Kilo registration probes. The installed tool versions checked for this record are `kilo 7.8.8` and `herdr 0.9.3`. These probes establish launch and prompt-delivery behavior only; they do not establish model conformance or verified trust.

The command lines below show the adapter-rendered forms where the Office dispatch record did not retain a literal process argv. `<probe-prompt>` and the Herdr agent/pane names are placeholders, not captured values. Probe outcomes are the observed 2026-10-14 results captured in the registration source notes and dispatch evidence.

## Headless `kilo run`

Rendered invocation (the prompt was appended as one argv element):

```sh
kilo run --model kilo/stepfun/step-5-preview-free --variant high --format json --pure --auto "<probe-prompt>"
```

Exit status was 0. The output contained a real model answer, a `ses_…` session id was observed in the JSON event stream, and the reported cost was 0. The free Kilo Gateway model ran without Kilo credentials. This is evidence that this model/effort launch worked, not evidence of model identity or conformance.

## Herdr pane dispatch

Herdr 0.9.3 started a Kilo pane agent with the registered pane form; the form pins the model and uses `--pure --auto`, with no top-level `--variant`:

```sh
herdr agent start <agent-name> --kind kilo --pane <pane> -- --model kilo/stepfun/step-5-preview-free --pure --auto
```

The agent was detected idle, the prompt landed, and a reply was received. The TUI maintains its own variant state (`ctrl+t`); this pane launch did not establish a variant value.

## Variant pass-through

Probe form:

```sh
kilo run --model kilo/stepfun/step-5-preview-free --variant <value> --format json --pure --auto "<probe-prompt>"
```

The probe passed both a known effort and a deliberately bogus value. Kilo accepted the argument without validating the variant value. Catalog rows therefore pin only effort values separately proven to launch; acceptance of an arbitrary value is not evidence that it changes model behavior.

## Autonomous shell permission

The shell-use probe used the headless form without `--auto` and asked the model to invoke `bash`:

```sh
kilo run --model kilo/stepfun/step-5-preview-free --variant high --format json --pure "<probe-prompt-that-uses-bash>"
```

Attempting `bash` produced an auto-rejected permission and the run exited 1. Kilo's reported guidance was to pass `--auto` for autonomous use. The registered worker and pane forms include `--auto`; it permits unprompted edits and commands in the disposable Office worktree.

## Top-level TUI variant rejection

```sh
kilo --variant high
```

The top-level CLI rejected `--variant`, printed help, and exited 1. The pane form omits this flag and leaves variant selection to the TUI.

## Trust and conformance status

Conformance remains pending. Trust remains `valid-unverified` until a trust act is recorded. Validation, model registration, and these smoke probes do not record a trust act or promote trust. Paid models were not tested; they require `kilo auth login`.
