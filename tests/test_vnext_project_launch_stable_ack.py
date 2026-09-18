from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = (ROOT / "browser_extension_vnext" / "content_adapter.js").read_text(encoding="utf-8")
POPUP = (ROOT / "browser_extension_vnext" / "popup.js").read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def test_transient_single_dom_match_is_not_enough_for_ack() -> None:
    verifier = _between(
        ADAPTER,
        "async function projectVerifyInsertedStable",
        "async function projectAutoSendInserted",
    )
    assert "const observations = 3;" in verifier
    loop = "for (let observation = 0; observation < observations; observation += 1)"
    assert loop in verifier
    assert "if (observation > 0) await projectDelay(150);" in verifier
    loop_body = verifier.split(loop, 1)[1]
    assert "const composer = projectFindComposer();" in loop_body
    assert "projectComposerText(composer) !== prompt" in loop_body


def test_stable_repeated_match_is_required_before_manual_ack() -> None:
    handle = _between(
        ADAPTER,
        "async function projectHandleLaunch",
        "async function projectInsertSelectedLaunch",
    )
    verify = handle.index("const stableInsertion = await projectVerifyInsertedStable")
    unstable = handle.index('return projectLaunchResult(false, "project_prompt_inserted_unverified"', verify)
    manual_ack = handle.rindex("const acknowledged = await projectAck(claimed.launch_id, claimId, conversationId);")
    assert verify < unstable < manual_ack
    assert 'return projectLaunchResult(true, "project_prompt_inserted"' in handle[manual_ack:]


def test_ack_failure_after_visible_insert_is_reported_separately() -> None:
    handle = _between(
        ADAPTER,
        "async function projectHandleLaunch",
        "async function projectInsertSelectedLaunch",
    )
    manual_ack = handle.rindex("const acknowledged = await projectAck(claimed.launch_id, claimId, conversationId);")
    tail = handle[manual_ack:]
    assert 'return projectLaunchResult(false, "project_prompt_ack_failed"' in tail
    assert 'return projectLaunchResult(false, "project_prompt_not_inserted"' not in tail


def test_manual_insert_propagates_structured_result_and_auto_poll_checks_ok() -> None:
    insert_selected = _between(
        ADAPTER,
        "async function projectInsertSelectedLaunch",
        "async function projectPoll",
    )
    poll = _between(
        ADAPTER,
        "async function projectPoll",
        'if (typeof chrome === "object"',
    )
    assert "return projectHandleLaunch(launch, { selectedByUser: true });" in insert_selected
    assert "const handled = await projectHandleLaunch(launch, { automatic: true });" in poll
    assert "if (!handled?.ok) return;" in poll


def test_popup_distinguishes_unverified_and_ack_failed_effects() -> None:
    assert "project_prompt_inserted_unverified" in POPUP
    assert "Nie wysyłaj go jeszcze." in POPUP
    assert "project_prompt_ack_failed" in POPUP
    assert "BDB nie potwierdził ACK" in POPUP
    assert 'project_prompt_not_inserted: "Prompt nie został wstawiony; niczego nie wysłano."' in POPUP
