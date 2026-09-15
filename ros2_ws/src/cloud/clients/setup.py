# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from setuptools import setup

package_name = "cloud_clients"

setup(
    name=package_name,
    version="0.1.0",
    packages=[
        "auth_client",
        "innate_proxy",
        "innate_proxy.adapters",
        "innate_llm",
        "innate_llm.wires",
        "training_client",
        "training_client.src",
    ],
    package_dir={
        "auth_client": "auth-client/auth_client",
        "innate_proxy": "proxy-client/innate_proxy",
        "innate_proxy.adapters": "proxy-client/innate_proxy/adapters",
        "innate_llm": "innate-llm/innate_llm",
        "innate_llm.wires": "innate-llm/innate_llm/wires",
        "training_client": "training-client/training_client",
        "training_client.src": "training-client/training_client/src",
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
    description="Innate cloud client libraries: auth, proxy, LLM vendors, and training.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "innate-auth-token = auth_client.__main__:main",
            "innate-training = training_client.cli:main",
        ],
    },
)
