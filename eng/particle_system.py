import taichi as ti
import numpy as np
import matplotlib as mpl
from eng.colormap import *
from functools import reduce    # 整数：累加；字符串、列表、元组：拼接。lambda为使用匿名函数

# TODO: --ok Unify all coordinate systems and put padding area outside the real world.
# TODO: --ok still warnings in NS, offset loop die for endless
# TODO: use hashgrid method to do NS
# TODO: better method of update kh

@ti.data_oriented
class ParticleSystem:
    def __init__(self, world, radius):
        print("Class Particle System starts to serve!")

        # Basic information of the simulation
        self.world = np.array(world)
        self.dim = len(world)
        assert self.dim in (2, 3), "SPH solver supports only 2D and 3D particle system and 2D ractangular world from ld_pos(0,0) now."

        # Material 材料类型定义
        self.material_fluid = 1
        self.material_soil = 2
        self.material_dummy = 10
        self.material_repulsive = 11

        # Basic particle property 粒子的基本属性
        self.particle_radius = radius
        self.particle_diameter = 2.0 * self.particle_radius
        self.kappa = 2.0
        self.kh = 1.2   # times the support domain radius to the particle radius. Should be adapted automaticlly soon
        self.smoothing_len = self.kh * self.particle_diameter
        self.support_radius = self.kappa * self.smoothing_len
        self.m_V = self.particle_diameter**self.dim     # m2 or m3 for cubic discrete
        self.particle_max_num = 2**16  # the max number of all particles, as 65536
        self.particle_max_num_per_cell = 100  # the max number of particles in each cell
        self.particle_max_num_neighbors = 100  # the max number of neighbour particles of each particle
        self.particle_num = ti.field(int, shape=())  # record the number of current particles

        # Grid property 背景格网的基本属性
        self.grid_size = ti.ceil(self.kappa * self.kh) * self.particle_diameter
        self.bound = [[-self.grid_size, -self.grid_size], [i + self.grid_size for i in world]]    # Simply create a rectangular range, down-left and up-right
        self.range = np.array([self.bound[1][0] - self.bound[0][0], self.bound[1][1] - self.bound[0][1]])    # Simply create a rectangular range
        self.grid_num = np.ceil(self.range / self.grid_size).astype(int)  # 格网总数
        self.grid_particles_num = ti.field(int)  # 每个格网中的粒子总数
        self.grid_particles = ti.field(int)  # 每个格网中的粒子编号

        # Particle related property 粒子携带的属性信息
        # Basic
        self.x = ti.Vector.field(self.dim, dtype=float)     # position
        self.pos2vis = ti.Vector.field(self.dim, dtype=float)   # position to visualization
        self.L = ti.Matrix.field(self.dim, self.dim, dtype=float)     # the normalised matrix
        self.val = ti.field(dtype=float)                      # store a value
        self.particle_neighbors_num = ti.field(int)         # total number of neighbour particles
        self.particle_neighbors = ti.field(int)             # index of neighbour particles
        self.material = ti.field(dtype=int)                 # material type
        # self.color = ti.field(dtype=int)                    # color in drawing for gui
        self.color = ti.Vector.field(3, dtype=float)     # color in drawing for ggui
        # Paras
        self.density = ti.field(dtype=float)
        self.u = ti.Vector.field(self.dim, dtype=float)
        self.stress = ti.Matrix.field(self.dim, self.dim, dtype=float)
        self.strain = ti.Matrix.field(self.dim, self.dim, dtype=float)

        # info of whole particle systems
        self.vmax = ti.field(float, shape=())
        self.vmin = ti.field(float, shape=())
        self.vmaxmax = ti.field(float, shape=())
        self.vminmin = ti.field(float, shape=())

        # Place nodes on root
        self.particles_node = ti.root.dense(ti.i, self.particle_max_num)    # 使用稠密数据结构开辟每个粒子数据的存储空间，按列存储
        self.particles_node.place(self.x, self.pos2vis, self.L, self.val, self.material, self.color)
        self.particles_node.place(self.density, self.u, self.stress, self.strain)
        self.particles_node.place(self.particle_neighbors_num)
        self.particle_node = self.particles_node.dense(ti.j, self.particle_max_num_neighbors)    # 使用稠密数据结构开辟每个粒子邻域粒子编号的存储空间，按行存储
        self.particle_node.place(self.particle_neighbors)

        grid_index = ti.ij if self.dim == 2 else ti.ijk          # 建立格网维度索引变量，xy or xyz
        grid_node = ti.root.dense(grid_index, self.grid_num)     # 使用稠密数据结构开辟每个格网中粒子总数的存储空间
        grid_node.place(self.grid_particles_num)
        cell_index = ti.k if self.dim == 2 else ti.l        # 建立粒子索引变量
        cell_node = grid_node.dense(cell_index, self.particle_max_num_per_cell)     # 使用稠密数据结构开辟每个格网中存储粒子编号的存储空间
        cell_node.place(self.grid_particles)

        # Create rectangle rangeary particles
        self.gen_rangeary_particles()

    ###########################################################################
    # NS
    ###########################################################################
    # 获取粒子位置对应的grid编号
    @ti.func
    def pos_to_index(self, pos):
        return ((pos - self.bound[0]) / self.grid_size).cast(int)

    @ti.func
    def is_valid_cell(self, cell):
        # Check whether the cell is in the grid
        flag = True
        for d in ti.static(range(self.dim)):
            flag = flag and (0 <= cell[d] < self.grid_num[d])
        return flag

    @ti.kernel
    def allocate_particles_to_grid(self):
        for p in range(self.particle_num[None]):
            cell = self.pos_to_index(self.x[p])
            offset = ti.atomic_add(self.grid_particles_num[cell], 1)
            self.grid_particles[cell, offset] = p

    @ti.kernel
    def search_neighbors(self):
        for p_i in range(self.particle_num[None]):
            # Skip rangeary particles
            if self.material[p_i] == self.material_dummy:
                continue
            center_cell = self.pos_to_index(self.x[p_i])
            cnt = 0
            for offset in ti.grouped(ti.ndrange(*((-1, 2),) * self.dim)):
                if cnt >= self.particle_max_num_neighbors:
                    break
                cell = center_cell + offset
                if not self.is_valid_cell(cell):
                    continue
                for j in range(self.grid_particles_num[cell]):
                    p_j = self.grid_particles[cell, j]
                    distance = (self.x[p_i] - self.x[p_j]).norm()
                    if p_i != p_j and distance < self.support_radius:
                        self.particle_neighbors[p_i, cnt] = p_j
                        cnt += 1
            self.particle_neighbors_num[p_i] = cnt

    ###########################################################################
    # Initialise and Update the particle system b
    ###########################################################################
    def initialize_particle_system(self):
        self.grid_particles_num.fill(0)
        self.particle_neighbors.fill(-1)
        self.allocate_particles_to_grid()
        self.search_neighbors()

    ###########################################################################
    # Add particles
    ###########################################################################
    # add one particle in p with given properties
    @ti.func
    def add_particle(self, p, val, x, u, density, stress, strain, material, color):
        self.val[p] = val
        self.x[p] = x
        self.u[p] = u
        self.density[p] = density
        self.stress[p] = stress
        self.strain[p] = strain
        self.material[p] = material
        self.color[p] = color

    # add particles with given properties
    @ti.kernel
    def add_particles(self, new_particles_num: int,
                      new_particles_value: ti.ext_arr(),
                      new_particles_positions: ti.ext_arr(),
                      new_particles_velocity: ti.ext_arr(),
                      new_particles_density: ti.ext_arr(),
                      new_particles_stress: ti.ext_arr(),
                      new_particles_strain: ti.ext_arr(),
                      new_particles_material: ti.ext_arr(),
                      new_particles_color: ti.ext_arr()):
        for p in range(self.particle_num[None],
                       self.particle_num[None] + new_particles_num):
            new_p = p - self.particle_num[None]
            x = ti.Vector.zero(float, self.dim)
            u = ti.Vector.zero(float, self.dim)
            stress = ti.Matrix.zero(float, self.dim, self.dim)
            strain = ti.Matrix.zero(float, self.dim, self.dim)
            color = ti.Vector.zero(float, 3)
            for d in ti.static(range(self.dim)):
                x[d] = new_particles_positions[new_p, d]
                u[d] = new_particles_velocity[new_p, d]
            for d, dd in ti.static(ti.ndrange(self.dim, self.dim)):
                stress[d, dd] = new_particles_stress[new_p, d, dd]
                strain[d, dd] = new_particles_strain[new_p, d, dd]

            for i in ti.static(range(3)):
                color[i] = new_particles_color[new_p, i]
            self.add_particle(p, new_particles_value[new_p], x, u,
                              new_particles_density[new_p], stress, strain,
                              new_particles_material[new_p], color)
        self.particle_num[None] += new_particles_num

    ###########################################################################
    # Generate boundary particles
    ###########################################################################
    # 增加 padding region 中所有方向上矩形边界的粒子，2d
    def gen_one_rangeary_cube(self, dl, tr, color, type, voff):
        self.add_cube(lower_corner=dl,
                      cube_size=tr - dl,
                      material=type,
                      color=color,
                      offset=voff)

    def gen_rangeary_particles(self):
        Dummy_color = (153/255, 153/255, 255/255)
        # Dummy_color = 0x9999FF
        Dummy_type = 10
        Dummy_off = self.particle_diameter
        Dummy_cube_d_dl = np.array(self.bound[0])
        Dummy_cube_d_tr = np.array([self.bound[1][0], 0])
        Dummy_cube_u_dl = np.array([self.bound[0][0], self.bound[1][1] - self.grid_size])
        Dummy_cube_u_tr = np.array(self.bound[1])
        Dummy_cube_l_dl = np.array([self.bound[0][0], 0])
        Dummy_cube_l_tr = np.array([0, self.bound[1][1] - self.grid_size])
        Dummy_cube_r_dl = np.array([self.bound[1][0] - self.grid_size, 0])
        Dummy_cube_r_tr = np.array([self.bound[1][0], self.bound[1][1] - self.grid_size])
        self.gen_one_rangeary_cube(Dummy_cube_d_dl, Dummy_cube_d_tr, Dummy_color, Dummy_type, Dummy_off)
        self.gen_one_rangeary_cube(Dummy_cube_u_dl, Dummy_cube_u_tr, Dummy_color, Dummy_type, Dummy_off)
        self.gen_one_rangeary_cube(Dummy_cube_l_dl, Dummy_cube_l_tr, Dummy_color, Dummy_type, Dummy_off)
        self.gen_one_rangeary_cube(Dummy_cube_r_dl, Dummy_cube_r_tr, Dummy_color, Dummy_type, Dummy_off)
        print("rangeary dummy particles' number: ", self.particle_num)

    ###########################################################################
    # Generate particles in rules
    ###########################################################################
    # add particles in a cube region
    def add_cube(self,
                 lower_corner,
                 cube_size,
                 material,
                 color=(1, 1, 1),
                 value=None,
                 velocity=None,
                 density=None,
                 stress=None,
                 strain=None,
                 offset=None):
        num_dim = []
        range_offset = offset if offset is not None else self.particle_diameter
        for i in range(self.dim):
            num_dim.append(np.arange(lower_corner[i] + self.particle_radius, lower_corner[i] + cube_size[i] + 1e-5, range_offset))
        num_new_particles = reduce(lambda x, y: x * y, [len(n) for n in num_dim])
        assert self.particle_num[None] + num_new_particles <= self.particle_max_num, 'My Error: exceed the maximum number of particles!'

        new_positions = np.array(np.meshgrid(*num_dim, sparse=False, indexing='ij' if self.dim == 2 else 'ijk'), dtype=np.float32)
        new_positions = new_positions.reshape(-1, reduce(lambda x, y: x * y, list(new_positions.shape[1:]))).transpose()
        print("New cube's number and dim: ", new_positions.shape)

        if color is None:
            color = np.zeros((num_new_particles, 3))
        else:
            color = np.array([color for _ in range(num_new_particles)], dtype=np.float32)
        if velocity is None:
            velocity = np.full_like(new_positions, 0)
        else:
            velocity = np.array([velocity for _ in range(num_new_particles)], dtype=np.float32)
        if stress is None:
            stress = np.array([np.zeros((self.dim, self.dim)) for _ in range(num_new_particles)], dtype=np.float32)
        else:
            stress = np.array([stress for _ in range(num_new_particles)], dtype=np.float32)
        if strain is None:
            strain = np.array([np.zeros((self.dim, self.dim)) for _ in range(num_new_particles)], dtype=np.float32)
        else:
            strain = np.array([strain for _ in range(num_new_particles)], dtype=np.float32)

        value = np.full_like(np.zeros(num_new_particles), value if value is not None else 0.0)
        density = np.full_like(np.zeros(num_new_particles), density if density is not None else 0.0)
        material = np.full_like(np.zeros(num_new_particles), material)
        self.add_particles(num_new_particles, value, new_positions, velocity, density, stress, strain, material, color)
        self.initialize_particle_system()

    ###########################################################################
    # Assist
    ###########################################################################
    @ti.kernel
    def copy2vis(self, s2w_ratio: float, max_res: int):
        for i in range(self.particle_num[None]):
            for j in ti.static(range(self.dim)):
                self.pos2vis[i][j] = (self.x[i][j] + self.grid_size) * s2w_ratio / max_res

    @ti.kernel
    def v_maxmin(self):
        vmax = -float('Inf')
        vmin = float('Inf')
        for i in range(self.particle_num[None]):
            if self.material[i] < 10:
                ti.atomic_max(vmax, self.val[i])
                ti.atomic_min(vmin, self.val[i])
        self.vmax[None] = vmax
        self.vmin[None] = vmin

    @ti.kernel
    def set_color(self):
        vrange1 = 1 / (self.vmax[None] - self.vmin[None])
        for i in range(self.particle_num[None]):
            if self.material[i] < 10:
                # self.color[i] = ti.Vector([1, (self.vmax[None] - self.val[i]) * vrange1, 0])  # Change the second value of RGB, from yellow to red
                tmp = (self.val[i] - self.vmin[None]) * vrange1
                self.color[i] = color_map(tmp)
