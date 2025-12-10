# Data Requirements

This directory contains scene data and assets required by the simulator.

## Required Datasets

### 1. ReplicaCAD Baked Lighting

**Path:** `data/ReplicaCAD_baked_lighting/`

Contains pre-baked lighting stages for the simulation environment.

**Required structure:**

```text
ReplicaCAD_baked_lighting/
├── stages_uncompressed/
│   └── Baked_sc0_staging_00.glb
└── configs/
    └── stages/
        └── Baked_sc0_staging_00.stage_config.json
```

**Download:** See [Habitat-Sim ReplicaCAD documentation](https://github.com/facebookresearch/habitat-sim/blob/main/DATASETS.md#replicacad)

### 2. ReplicaCAD Dataset

**Path:** `data/ReplicaCAD_dataset/`

Contains object meshes used for collision detection and scene composition.

**Required structure:**

```text
ReplicaCAD_dataset/
└── objects/
    ├── frl_apartment_wall_cabinet_02.glb
    ├── frl_apartment_tvstand.glb
    ├── frl_apartment_table_01.glb
    ├── frl_apartment_table_02.glb
    ├── frl_apartment_table_03.glb
    ├── frl_apartment_sofa.glb
    ├── frl_apartment_chair_01.glb
    └── ... (other objects)
```

**Download:** See [Habitat-Sim ReplicaCAD documentation](https://github.com/facebookresearch/habitat-sim/blob/main/DATASETS.md#replicacad)

## Quick Download (if using Habitat tools)

```bash
# Using habitat-sim's dataset downloader
python -m habitat_sim.utils.datasets_download --uids replica_cad_baked_lighting replica_cad_dataset --data-path data/
```

Or download manually from [Hugging Face](https://huggingface.co/datasets/ai-habitat/ReplicaCAD_baked_lighting).

## Other Data

- `assets/` - Robot URDFs and other simulation assets
- `environments/` - Environment configuration JSON files
- `urdf/` - Robot description files
