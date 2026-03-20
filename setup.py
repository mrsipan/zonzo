import setuptools

setuptools.setup(
    name="zonzo",
    version="0.0.1",
    description="based on bobo",
    author="Benjamin Sanchez",
    py_modules=["zonzo"],
    package_dir={"": "."},
    install_requires=[
        "webob",
        ],
    python_requires=">=3.8",
    )
