from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_marmoset.bridge import call_host


@skill_entry
def main(object_uid: str = "", **_kwargs):
    return skill_success(
        "Marmoset Toolbag camera framed.",
        **call_host("scene.frame", {"object_uid": object_uid}),
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
