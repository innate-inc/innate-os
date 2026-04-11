import glob
from setuptools import setup, find_packages

package_name = "cloud_clients"

# Collect pre-built frontend assets for the training manager web UI.
# These live under training-manager/training_manager/static/ after
# running ``npm run build`` in the frontend/ directory.
_static_root = "training-manager/training_manager/static"
_static_files = glob.glob(f"{_static_root}/**/*", recursive=True)
_package_data_static = [
    f.replace("training-manager/training_manager/", "")
    for f in _static_files
    if not f.endswith("/")
]

setup(
    name=package_name,
    version="0.1.0",
    packages=[
        "auth_client",
        "innate_proxy",
        "innate_proxy.adapters",
        "training_client",
        "training_client.src",
        "training_manager",
        "training_manager.api",
    ],
    package_dir={
        "auth_client": "auth-client/auth_client",
        "innate_proxy": "proxy-client/innate_proxy",
        "innate_proxy.adapters": "proxy-client/innate_proxy/adapters",
        "training_client": "training-client/training_client",
        "training_client.src": "training-client/training_client/src",
        "training_manager": "training-manager/training_manager",
        "training_manager.api": "training-manager/training_manager/api",
    },
    package_data={
        "training_manager": _package_data_static,
    },
    data_files=[
        (
            "share/ament_index/resource_index/packages",
            ["resource/" + package_name],
        ),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Innate Engineering",
    maintainer_email="eng@innate.bot",
    description="Innate cloud client libraries: auth, proxy, training, and training manager UI.",
    license="Proprietary",
    entry_points={
        "console_scripts": [
            "innate-auth-token = auth_client.__main__:main",
            "innate-training = training_client.cli:main",
            "training-manager = training_manager.server:main",
        ],
    },
)
