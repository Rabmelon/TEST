import taichi as ti
from eng.gguishow import *
from eng.particle_system import *
from eng.wcsesph import *

ti.init(arch=ti.cpu, debug=True)
# ti.init(arch=ti.cuda, packed=True, device_memory_fraction=0.75)     # MEMORY max 4G in GUT, 6G in Legion

if __name__ == "__main__":
    print("hallo tiSPHi TEST!")

    # init particle system paras, world unit is cm (BUT not cm actually! maybe still m)
    screen_to_world_ratio = 800   # exp: world = (150, 100), ratio = 4, screen res = (600, 400)
    rec_world = [0.584, 0.8]   # a rectangle world start from (0, 0) to this pos
    particle_radius = 0.002
    cube_size = [0.146, 0.292]

    case1 = ParticleSystem(rec_world, particle_radius)
    case1.add_cube(lower_corner=[0.0, 0], cube_size=cube_size, material=1, density=1000.0)

    solver = WCSESPHSolver(case1, TDmethod=1, kernel=2, visco=0.00005, stiff=50000, expo=7)

    gguishow(case1, solver, rec_world, screen_to_world_ratio, stepwise=10, iparticle=None, color_title="density N/m3", kradius=1.5, write_to_disk=0, pause=False)
    # color title: pressure Pa; velocity m/s; density N/m3; d density N/m3/s;
    # * SPACE for pause/run, ESC for terminate, left click for showing the info of position and grid index
