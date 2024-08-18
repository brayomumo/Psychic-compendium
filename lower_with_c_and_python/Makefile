# Compile main.c to  a shared object
compile:
	cc -fPIC -shared -o libsum.so main.c

run_py: compile
	python main.py
