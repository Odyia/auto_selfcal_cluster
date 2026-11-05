from setuptools import find_packages, setup


setup(
    name="vvisor",
    version="0.1.0",
    description="VLA visibility optimization and self-calibration orchestration runner",
    packages=find_packages(include=["vvisor", "vvisor.*"]),
    python_requires=">=3.8",
    entry_points={
        "console_scripts": [
            "vvisor=vvisor.cli:main",
            "auto-calibration=vvisor.cli:main",
        ]
    },
)
