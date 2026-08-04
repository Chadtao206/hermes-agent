"""Behavioral tests for fail-closed blocked-recovery controls."""

from hermes_cli.kanban_recovery_config import RecoveryControl


def test_empty_managed_boards_disables_mutation():
    control = RecoveryControl.from_config(
        {"kanban": {"blocked_recovery": {"managed_boards": []}}}
    )

    assert control.mutation_enabled is False
    assert control.errors == ("managed_boards must contain at least one known board",)


def test_new_board_cannot_start_live():
    previous = RecoveryControl.from_config(
        {"kanban": {"blocked_recovery": {"managed_boards": {"default": {}}}}}
    )
    raw = {
        "kanban": {
            "blocked_recovery": {
                "managed_boards": {
                    "default": {"review_mode": "live", "transient_mode": "shadow"},
                    "new": {"review_mode": "live", "transient_mode": "live"},
                }
            }
        }
    }

    control = RecoveryControl.from_config(raw, previous=previous)

    assert control.boards["new"].review_mode == "shadow"
    assert control.boards["new"].transient_mode == "shadow"
    assert control.boards["new"].clean_censuses_required == 2


def test_transient_live_cannot_lead_review_live():
    control = RecoveryControl.from_config(
        {
            "kanban": {
                "blocked_recovery": {
                    "managed_boards": {
                        "default": {"review_mode": "shadow", "transient_mode": "live"}
                    }
                }
            }
        }
    )

    assert control.mutation_enabled is False
    assert "transient_mode cannot be live before review_mode" in control.errors[0]


def test_controller_manifest_requires_all_exact_fields():
    control = RecoveryControl.from_config(
        {
            "kanban": {
                "blocked_recovery": {
                    "managed_boards": {"default": {}},
                    "controller_jobs": {"router": {"job_id": "fef26eedc8da"}},
                }
            }
        }
    )

    assert control.mutation_enabled is False
    assert "router.source_manifest_digest" in control.errors[0]


def test_default_controls_are_shadow_and_have_bounded_retries():
    from hermes_cli.config import DEFAULT_CONFIG

    control = RecoveryControl.from_config(DEFAULT_CONFIG)

    assert control.mutation_enabled is True
    assert control.boards["default"].review_mode == "shadow"
    assert control.boards["default"].transient_mode == "shadow"
    assert control.verifier_retry_delays_seconds == (300, 900, 1800, 3600)
    assert control.judge_retry_delays_seconds == (300, 900, 1800, 3600)
    assert control.router_lease_seconds == control.target_stability_seconds == 300
    assert control.verification_deadline_seconds == control.judge_deadline_seconds == 7200
    assert control.mutation_cap == 1
    assert control.rollout_mode == "shadow"


def test_guarded_control_update_requires_current_digest(tmp_path, monkeypatch):
    from hermes_cli.kanban_recovery_config import (
        RecoveryControlUpdate,
        guarded_update_recovery_control,
        load_recovery_control,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    current = load_recovery_control()
    stale = guarded_update_recovery_control(
        RecoveryControlUpdate(
            expected_digest="0" * 64,
            operation="set_board",
            values={"board": "default", "review_mode": "live", "transient_mode": "shadow"},
        )
    )

    assert stale.outcome == "precondition_changed"
    assert load_recovery_control().digest() == current.digest()


def test_control_update_writes_exact_review_and_transient_modes(tmp_path, monkeypatch):
    from hermes_cli.kanban_recovery_config import (
        RecoveryControlUpdate,
        guarded_update_recovery_control,
        load_recovery_control,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "kanban:\n  blocked_recovery:\n    rollout_mode: live\n", encoding="utf-8"
    )
    current = load_recovery_control()
    result = guarded_update_recovery_control(
        RecoveryControlUpdate(
            expected_digest=current.digest(),
            operation="set_board",
            values={"board": "default", "review_mode": "live", "transient_mode": "shadow"},
        )
    )

    assert result.outcome == "committed"
    assert load_recovery_control().boards["default"].review_mode == "live"
    assert load_recovery_control().boards["default"].transient_mode == "shadow"


def test_allowlist_update_preserves_unrelated_yaml(tmp_path, monkeypatch):
    from hermes_cli.kanban_recovery_config import (
        RecoveryControlUpdate,
        guarded_update_recovery_control,
        load_recovery_control,
    )
    import yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("display:\n  skin: midnight\n", encoding="utf-8")
    current = load_recovery_control()
    result = guarded_update_recovery_control(
        RecoveryControlUpdate(
            expected_digest=current.digest(),
            operation="set_review_allowlist",
            values={"board": "default", "review_allowlist": ["reviewer-a", "reviewer-b"]},
        )
    )

    assert result.outcome == "committed"
    assert load_recovery_control().boards["default"].review_allowlist == ("reviewer-a", "reviewer-b")
    assert yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))["display"]["skin"] == "midnight"


def test_controller_update_requires_and_writes_complete_manifest(tmp_path, monkeypatch):
    from hermes_cli.kanban_recovery_config import (
        RecoveryControlUpdate,
        guarded_update_recovery_control,
        load_recovery_control,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    current = load_recovery_control()
    result = guarded_update_recovery_control(
        RecoveryControlUpdate(
            expected_digest=current.digest(),
            operation="set_controller",
            values={
                "controller_role": "router",
                "role": "router",
                "job_id": "fef26eedc8da",
                "role_tag": "blocked-recovery-router",
                "source_manifest": ["scripts/review_delegator/runner.py"],
                "source_manifest_digest": "a" * 64,
                "schedule_digest": "b" * 64,
                "delivery_scope": "kanban:default",
                "desired_mode": "shadow",
            },
        )
    )

    assert result.outcome == "committed"
    assert load_recovery_control().controller_jobs["router"].desired_mode == "shadow"


def test_malformed_board_control_disables_only_recovery_mutation():
    control = RecoveryControl.from_config(
        {
            "kanban": {
                "blocked_recovery": {
                    "managed_boards": {"default": {"transient_retry_budget": "two"}}
                }
            }
        }
    )

    assert control.mutation_enabled is False
    assert control.errors == ("default.transient_retry_budget must be a positive integer",)


def test_guarded_update_cannot_persist_live_modes_for_new_board(tmp_path, monkeypatch):
    from hermes_cli.kanban_recovery_config import (
        RecoveryControlUpdate,
        guarded_update_recovery_control,
        load_recovery_control,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    current = load_recovery_control()
    result = guarded_update_recovery_control(
        RecoveryControlUpdate(
            expected_digest=current.digest(),
            operation="set_board",
            values={"board": "new", "review_mode": "live", "transient_mode": "live"},
        )
    )

    assert result.outcome == "committed"
    assert load_recovery_control().boards["new"].review_mode == "shadow"
    assert load_recovery_control().boards["new"].transient_mode == "shadow"


def test_shadow_or_canary_rollout_rejects_mutation_cap_above_one():
    control = RecoveryControl.from_config(
        {
            "kanban": {
                "blocked_recovery": {
                    "managed_boards": {"default": {}},
                    "rollout_mode": "canary",
                    "mutation_cap": 2,
                }
            }
        }
    )

    assert control.mutation_enabled is False
    assert control.errors == ("mutation_cap must be 1 while rollout_mode is shadow or canary",)


def test_canary_rollout_authorizes_only_existing_board_review_live():
    previous = RecoveryControl.from_config(
        {
            "kanban": {
                "blocked_recovery": {
                    "rollout_mode": "live",
                    "managed_boards": {
                        "default": {
                            "review_mode": "shadow",
                            "transient_mode": "shadow",
                        }
                    },
                }
            }
        }
    )
    control = RecoveryControl.from_config(
        {
            "kanban": {
                "blocked_recovery": {
                    "rollout_mode": "canary",
                    "mutation_cap": 1,
                    "managed_boards": {
                        "default": {
                            "review_mode": "live",
                            "transient_mode": "shadow",
                            "review_allowlist": ["reviewer"],
                        },
                        "new": {
                            "review_mode": "live",
                            "transient_mode": "shadow",
                            "review_allowlist": ["reviewer"],
                        },
                    },
                }
            }
        },
        previous=previous,
    )

    assert control.mutation_enabled is True
    assert control.boards["default"].review_mode == "live"
    assert control.boards["default"].transient_mode == "shadow"
    assert control.boards["new"].review_mode == "shadow"
    assert control.mutation_cap == 1


def test_canary_rollout_rejects_transient_live_even_after_review_live():
    control = RecoveryControl.from_config(
        {
            "kanban": {
                "blocked_recovery": {
                    "rollout_mode": "canary",
                    "mutation_cap": 1,
                    "managed_boards": {
                        "default": {
                            "review_mode": "live",
                            "transient_mode": "live",
                            "review_allowlist": ["reviewer"],
                        }
                    },
                }
            }
        }
    )

    assert control.mutation_enabled is False
    assert "default.transient_mode cannot be live while rollout_mode is canary" in control.errors


def test_invalid_transition_leaves_recovery_control_unchanged(tmp_path, monkeypatch):
    from hermes_cli.kanban_recovery_config import (
        RecoveryControlUpdate,
        guarded_update_recovery_control,
        load_recovery_control,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    current = load_recovery_control()
    result = guarded_update_recovery_control(
        RecoveryControlUpdate(
            expected_digest=current.digest(),
            operation="set_board",
            values={"board": "default", "review_mode": "shadow", "transient_mode": "live"},
        )
    )

    assert result.outcome == "invalid"
    assert load_recovery_control().digest() == current.digest()


def test_guarded_update_rejects_unrelated_invalid_full_config(tmp_path, monkeypatch):
    from hermes_cli.kanban_recovery_config import (
        RecoveryControlUpdate,
        guarded_update_recovery_control,
        load_recovery_control,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text("fallback_model: bad-string\n", encoding="utf-8")
    before = config_path.read_text(encoding="utf-8")
    current = load_recovery_control()

    result = guarded_update_recovery_control(
        RecoveryControlUpdate(
            expected_digest=current.digest(),
            operation="set_review_allowlist",
            values={"board": "default", "review_allowlist": ["reviewer-a"]},
        )
    )

    assert result.outcome == "invalid"
    assert config_path.read_text(encoding="utf-8") == before
    assert load_recovery_control().digest() == current.digest()


def test_shadow_rollout_disables_all_live_recovery_mutation_modes():
    control = RecoveryControl.from_config(
        {
            "kanban": {
                "blocked_recovery": {
                    "rollout_mode": "shadow",
                    "managed_boards": {
                        "default": {"review_mode": "live", "transient_mode": "live"}
                    },
                    "controller_jobs": {
                        "router": {
                            "role": "router",
                            "job_id": "fef26eedc8da",
                            "role_tag": "blocked-recovery-router",
                            "source_manifest": ["scripts/review_delegator/runner.py"],
                            "source_manifest_digest": "a" * 64,
                            "schedule_digest": "b" * 64,
                            "delivery_scope": "kanban:default",
                            "desired_mode": "live",
                        }
                    },
                }
            }
        }
    )

    assert control.mutation_enabled is False
    assert "default.review_mode cannot be live while rollout_mode is shadow" in control.errors
    assert "router.desired_mode cannot be live while rollout_mode is shadow" in control.errors


def test_shared_control_uses_default_root_not_active_profile(tmp_path, monkeypatch):
    from hermes_cli.kanban_recovery_config import load_recovery_control

    root = tmp_path / "shared-root"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    (root / "config.yaml").write_text(
        "kanban:\n  blocked_recovery:\n    rollout_mode: live\n", encoding="utf-8"
    )
    (profile / "config.yaml").write_text(
        "kanban:\n  blocked_recovery:\n    rollout_mode: shadow\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(profile))

    assert load_recovery_control().rollout_mode == "live"


def test_guarded_update_writes_shared_root_not_active_profile(tmp_path, monkeypatch):
    from hermes_cli.kanban_recovery_config import (
        RecoveryControlUpdate,
        guarded_update_recovery_control,
        load_recovery_control,
    )

    root = tmp_path / "shared-root"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    root_config = root / "config.yaml"
    profile_config = profile / "config.yaml"
    root_config.write_text(
        "kanban:\n  blocked_recovery:\n    rollout_mode: live\n", encoding="utf-8"
    )
    profile_config.write_text("display:\n  skin: midnight\n", encoding="utf-8")
    profile_before = profile_config.read_text(encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    current = load_recovery_control()

    result = guarded_update_recovery_control(
        RecoveryControlUpdate(
            expected_digest=current.digest(),
            operation="set_board",
            values={"board": "default", "review_mode": "live", "transient_mode": "shadow"},
        )
    )

    assert result.outcome == "committed"
    assert load_recovery_control().boards["default"].review_mode == "live"
    assert "review_mode: live" in root_config.read_text(encoding="utf-8")
    assert profile_config.read_text(encoding="utf-8") == profile_before


def test_cold_load_downgrades_unadmitted_board_live_modes(tmp_path, monkeypatch):
    from hermes_cli.kanban_recovery_config import load_recovery_control

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "kanban:\n"
        "  blocked_recovery:\n"
        "    rollout_mode: live\n"
        "    managed_boards:\n"
        "      default: {}\n"
        "      new-board:\n"
        "        review_mode: live\n"
        "        transient_mode: live\n",
        encoding="utf-8",
    )

    control = load_recovery_control()

    assert control.boards["new-board"].review_mode == "shadow"
    assert control.boards["new-board"].transient_mode == "shadow"
