.PHONY: check install

check:
	python3 -m compileall -q gc_profiled.py scripts tests
	python3 -m unittest discover -s tests -v
	git diff --check

install:
	python3 scripts/install.py --enable
