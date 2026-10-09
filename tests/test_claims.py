"""Action claims reserve each id exactly once and survive restarts."""

from revenue_agents.guardrails.claims import ActionClaims


def test_an_id_can_only_be_claimed_once(tmp_path):
    claims = ActionClaims(tmp_path)
    assert claims.claim("email:D-001:first") is True
    assert claims.claim("email:D-001:first") is False
    assert ActionClaims(tmp_path).claim("email:D-001:first") is False  # another process, same folder


def test_released_claim_can_be_claimed_again(tmp_path):
    claims = ActionClaims(tmp_path)
    claims.claim("a")
    claims.release("a")
    assert claims.claim("a") is True


def test_in_progress_lists_claims_that_never_completed(tmp_path):
    claims = ActionClaims(tmp_path)
    claims.claim("done")
    claims.complete("done")
    claims.claim("stuck:after:crash")
    (tmp_path / "half-written.json").touch()  # crash between create and write

    assert claims.in_progress() == ["half-written", "stuck:after:crash"]


def test_ids_with_special_characters_are_safe_file_names(tmp_path):
    claims = ActionClaims(tmp_path)
    assert claims.claim("../../etc/passwd")
    assert claims.exists("../../etc/passwd")
    assert [path.parent for path in tmp_path.iterdir()] == [tmp_path]
