from office.verification_coverage import uncredited_validate


def test_full_flag_is_flagged_because_land_credits_only_the_bare_script():
    assert uncredited_validate(["scripts/validate.sh --full"]) == ["scripts/validate.sh --full"]
    assert uncredited_validate(["scripts/validate.sh", "scripts/validate.sh --pr"]) == []
