from setuptools import setup, find_packages

setup(
    name="tableau-to-pbi-accelerator",
    version="1.0.0",
    description="Tableau to Power BI Migration Accelerator — Mastech Digital",
    packages=find_packages(exclude=["tests*"]),
    python_requires=">=3.11",
    install_requires=[
        "lxml>=4.9.0",
        "pydantic>=2.0.0",
        "click>=8.1.0",
        "lark>=1.1.0",
        "networkx>=3.0",
        "anthropic>=0.30.0",
        "pyyaml>=6.0",
        "rich>=13.0.0",
        "python-dotenv>=1.0.0",
    ],
    extras_require={
        "api": ["fastapi>=0.110.0", "uvicorn>=0.29.0", "python-multipart>=0.0.9"],
        "dev": ["pytest>=8.0.0", "pytest-asyncio>=0.23.0"],
    },
    entry_points={
        "console_scripts": [
            "accelerator=cli:cli",
        ]
    },
    package_data={
        "": ["config/*.yaml"],
    },
    include_package_data=True,
)
