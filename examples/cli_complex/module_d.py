import core


def function_d():
    print("Called from module_d, single file")
    core.module_g.function_g()
