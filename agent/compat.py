"""Local display of the server's compatibility answer (agent_welcome.compat).

Only values from fixed tables are printed; any other text from the peer is ignored, never echoed. The answer is
what the server derived from this agent's *declaration*; it is not a verification, and the text says so.
"""

STATUS = {
    "compatible": "declared capabilities accepted by the server. Whether source_epoch really arrives is checked "
                  "by the server from the received data and is not confirmed here",
    "limited": "restricted functions",
}
LIMITATIONS = {
    "source_epoch_not_declared": "no source_epoch support declared: lap tyre/fuel checkpoints and lapped "
                                 "intervals stay unavailable",
    "agent_info_invalid": "the agent identification was not understood by the server",
}


def compat_lines(welcome):
    """Lines to print after the handshake. An older server sends no answer; that is reported as unknown."""
    answer = welcome.get("compat") if isinstance(welcome, dict) else None
    if not isinstance(answer, dict) or answer.get("status") not in STATUS:
        return ["Server compatibility: not reported by this server (unknown)"]
    lines = [f"Server compatibility: {answer['status']} - {STATUS[answer['status']]}"]
    found = answer.get("limitations")
    for code in found if isinstance(found, list) else []:
        if isinstance(code, str) and code in LIMITATIONS:
            lines.append(f"  - {LIMITATIONS[code]}")
    return lines
