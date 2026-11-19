# Built-in rules

Run `ansreview rules` for the list. The AR checks are deterministic and fast;
they catch security problems ansible-lint doesn't.

| Rule | Title | What |
|---|---|---|
| AR000 | YAML parse error | The file is not valid YAML |
| AR001 | Hard-coded secret | Secret in vars, defaults or module args. `!vault` and `{{ }}` are fine. [`fix`](../usage/fix.md#2-built-in-fixes) can vault it |
| AR002 | Secret argument without `no_log` | Task passes a password/token argument without `no_log: true`. `fix` adds it |
| AR003 | TLS validation disabled | `validate_certs: false` and friends |
| AR005 | `curl` piped into `sh` | A download piped straight into a shell |
| AR006 | World-writable permissions | `mode: 0777` and the like. `fix` sets `0644` / `0755` |
| AR007 | Plain HTTP download or repository | |
| AR008 | `ignore_errors` without `failed_when` | |
| AR009 | `get_url` without checksum | |
| AR011 | SELinux / firewall / audit weakened | SELinux disabled or permissive, firewalld or auditd stopped |
| AR012 | Weak sshd configuration | `PermitRootLogin yes` / `PasswordAuthentication yes` written to sshd_config |
| AR013 | `sudo` inside shell/command | Use `become` instead |
| AR014 | Credentials embedded in URL | `https://user:pass@…` |
| AR015 | `debug` prints a secret | `fix` adds `no_log: true` |
| AR016 | Private key committed | |
| AR017 | Repository `gpgcheck` disabled | |

## Other findings

- **ansible-lint** findings keep their own rule IDs, such as `fqcn[action-core]`
  or `yaml[line-length]`.
- **Model** findings use the `llm:<rule>` prefix and carry a confidence score
  between 0 and 1. `min_confidence` drops the unsure ones.

## Switching rules off

```yaml
disabled_rules: [AR009, yaml[line-length]]
```

or `--disable AR009` on the command line (repeatable). Both AR and ansible-lint
IDs work.

## Severities

`info` < `low` < `medium` < `high` < `critical`. `min_severity` hides findings
below a level, and `fail_on` sets the level at which the exit code becomes `1`.
