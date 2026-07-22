# Guardian SRE

## Repository verification

Verification is supported on Linux/WSL `amd64`. From a fresh checkout, run
exactly:

```bash
./scripts/bootstrap.sh
.tools/bin/task <target>
```

Bootstrap installs checksum-verified, repository-local pinned versions of
`uv` and Task under the ignored `.tools/bin` directory, then synchronizes the
locked Python development environment. It does not install system packages or
modify your shell configuration. Host prerequisites are `bash`, `curl`, `tar`,
and either `sha256sum` or `shasum -a 256`.

`[prerequisite]` means setup is incomplete or a pinned tool is absent or has
the wrong version. `[baseline]` means the toolchain is operational but a
required suite or environment harness is not implemented. Native Ruff,
Pyright, pytest, and traceability-checker failures are also nonzero baseline
evidence; none is converted into a pass or skip.

See `Taskfile.yml` for target definitions and
`docs/verification-baseline.md` for the complete target inventory, current
observations, and the no-green reporting rule.
