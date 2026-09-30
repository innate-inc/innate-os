from glob import glob

from setuptools import setup

package_name = "mars_sim_driver"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/launch", glob("launch/*.launch.py")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    description="Virtual MARS: headless MuJoCo sim impersonating the hardware drivers",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "sim_driver = mars_sim_driver.node:main",
            "world_server = mars_sim_driver.world_server:main",
            "grid_localizer_sim = mars_sim_driver.grid_localizer_sim:main",
        ],
    },
)
