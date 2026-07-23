#!/usr/bin/env bash

guardian_validate_run_id() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] && [[ "$1" != *..* ]]
}
