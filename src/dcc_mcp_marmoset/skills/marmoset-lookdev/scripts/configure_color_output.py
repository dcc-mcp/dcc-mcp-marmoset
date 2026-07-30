from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_marmoset.bridge import call_host


@skill_entry
def main(tone_mapping: str = "aces", exposure: float = 1.0, **_kwargs):
    return skill_success(
        "Marmoset Toolbag color output configured.",
        **call_host(
            "camera.configure_color_output",
            {"tone_mapping": tone_mapping, "exposure": exposure},
        ),
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
