# Dirty Football CC0 PBR showcase

This showcase uses Poly Haven's [Dirty Football](https://polyhaven.com/a/dirty_football),
created by Rohit Seervi and published under Poly Haven's
[CC0 asset license](https://polyhaven.com/license). Source files were selected
through the public API using this identifying User-Agent:

```text
dcc-mcp-marmoset-showcase/0.1 (+https://github.com/dcc-mcp/dcc-mcp-marmoset)
```

## Local source verification

| File | MD5 |
| --- | --- |
| `dirty_football_2k.fbx` | `f0c29826f30fac30c6ab46f730df5233` |
| `dirty_football_diff_2k.jpg` | `a05f7729e97cca7ddd3b6ccbf67f28ba` |
| `dirty_football_nor_dx_2k.jpg` | `5317b8c20350cfd0504851e797be261b` |
| `dirty_football_rough_2k.jpg` | `daa39d83b0823f564d97ac128d59a0ab` |
| `dirty_football_ao_2k.jpg` | `9713004bb44ea1b896fbd87f6f47bdac` |
| generated non-metal map `nonmetal_black.png` | `1f26deb7ac319f1815ba3728ea101998` |

The large source files and generated `.tbscene` remain local test artifacts;
the repository distributes only the optimized real-render showcase image.

## Live Toolbag validation

- Toolbag `5.02` (`5022`), NVIDIA GeForce RTX 5080, DXR.
- One model imported and the material assigned to four LOD mesh children.
- Five PBR maps configured with Albedo in sRGB and data maps in linear space.
- `marmoset_diagnostics__validate_assets`: 5 texture references checked, 0 missing.
- Scene saved and camera rendered at 1920x1080 with 64 samples.

The final WebP is a composition of the real Toolbag render and three of its
actual input maps. No generated application UI or synthetic render result is used.
