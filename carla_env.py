import atexit
import os
import signal
import sys
import carla
import gym
import time
import random
import numpy as np
import math
from queue import Queue
from misc import dist_to_roadline, exist_intersection
from gym import spaces
from setup import setup
from absl import logging
import graphics
import pygame
logging.set_verbosity(logging.INFO)

# Carla environment
class CarlaEnv(gym.Env):

    metadata = {'render.modes': ['human']}

    def __init__(self, town, fps, im_width, im_height, repeat_action, start_transform_type, sensors,
                 action_type, enable_preview, steps_per_episode, playing=False, timeout=60):
        super(CarlaEnv, self).__init__()

        self.client, self.world, self.frame, self.server = setup(town=town, fps=fps, client_timeout=timeout)
        self.client.set_timeout(5.0)
        self.map = self.world.get_map()
        blueprint_library = self.world.get_blueprint_library()
        self.lincoln = blueprint_library.filter('lincoln')[0]
        self.im_width = im_width
        self.im_height = im_height
        self.repeat_action = repeat_action
        self.action_type = action_type
        self.start_transform_type = start_transform_type
        self.sensors = sensors
        self.actor_list = []
        self.preview_camera = None
        self.steps_per_episode = steps_per_episode
        self.playing = playing
        self.preview_camera_enabled = enable_preview
        
        # self.episode = 0
        

    @property
    def observation_space(self, *args, **kwargs):
        """Returns the observation spec of the sensor."""
        return gym.spaces.Box(low=0.0, high=255.0, shape=(self.im_height, self.im_width, 3), dtype=np.uint8)

    @property
    def action_space(self):
        """Returns the expected action passed to the `step` method."""
        if self.action_type == 'continuous':
            return gym.spaces.Box(low=np.array([0.0, -1.0]), high=np.array([1.0, 1.0]))
        elif self.action_type == 'discrete':
            return gym.spaces.MultiDiscrete([4, 9])
        else:
            raise NotImplementedError()
        # TODO: Add discrete actions (here and anywhere else required)


    def seed(self, seed):
        if not seed:
            seed = 7
        random.seed(seed)
        self._np_random = np.random.RandomState(seed) 
        return seed

    # Resets environment for new episode
    def reset(self):
        self._destroy_agents()
        # logging.debug("Resetting environment")
        # Car, sensors, etc. We create them every episode then destroy
        self.collision_hist = []
        self.lane_invasion_hist = []
        self.actor_list = []
        self.frame_step = 0
        self.out_of_loop = 0
        self.dist_from_start = 0

        # self.total_reward = 0

        self.front_image_Queue = Queue()
        self.preview_image_Queue = Queue()

        # self.episode += 1

        # When Carla breaks (stopps working) or spawn point is already occupied, spawning a car throws an exception
        # We allow it to try for 3 seconds then forgive
        spawn_start = time.time()
        while True:
            try:
                # Get random spot from a list from predefined spots and try to spawn a car there
                self.start_transform = self._get_start_transform()
                self.end_transform = self._get_end_tranform()
                self.prev_dist = self.start_transform.location.distance(self.end_transform.location)
                self.curr_loc = self.start_transform.location
                self.vehicle = self.world.spawn_actor(self.lincoln, self.start_transform)
                break
            except Exception as e:
                logging.error('Error carla 141 {}'.format(str(e)))
                time.sleep(0.01)

            # If that can't be done in 3 seconds - forgive (and allow main process to handle for this problem)
            if time.time() > spawn_start + 3:
                raise Exception('Can\'t spawn a car')

        # Append actor to a list of spawned actors, we need to remove them later
        self.actor_list.append(self.vehicle)

        # TODO: combine the sensors
        if 'rgb' in self.sensors:
            self.rgb_cam = self.world.get_blueprint_library().find('sensor.camera.rgb')
        elif 'semantic' in self.sensors:
            self.rgb_cam = self.world.get_blueprint_library().find('sensor.camera.semantic_segmentation')
        else:
            raise NotImplementedError('unknown sensor type')

        self.rgb_cam.set_attribute('image_size_x', f'{self.im_width}')
        self.rgb_cam.set_attribute('image_size_y', f'{self.im_height}')
        self.rgb_cam.set_attribute('fov', '90')

        bound_x = self.vehicle.bounding_box.extent.x
        bound_y = self.vehicle.bounding_box.extent.y


        transform_front = carla.Transform(carla.Location(x=bound_x, z=1.0))
        self.sensor_front = self.world.spawn_actor(self.rgb_cam, transform_front, attach_to=self.vehicle)
        self.sensor_front.listen(self.front_image_Queue.put)
        self.actor_list.extend([self.sensor_front])

        # Preview ("above the car") camera
        if self.preview_camera_enabled:
            # TODO: add the configs
            self.preview_cam = self.world.get_blueprint_library().find('sensor.camera.rgb')
            self.preview_cam.set_attribute('image_size_x', '400')
            self.preview_cam.set_attribute('image_size_y', '400')
            self.preview_cam.set_attribute('fov', '100')
            transform = carla.Transform(carla.Location(x=-5.5, z=2.5), carla.Rotation(pitch=8.0))
            self.preview_sensor = self.world.spawn_actor(self.preview_cam, transform, attach_to=self.vehicle, attachment_type=carla.AttachmentType.SpringArm)
            self.preview_sensor.listen(self.preview_image_Queue.put)
            self.actor_list.append(self.preview_sensor)

        # Here's some workarounds.
        self.vehicle.apply_control(carla.VehicleControl(throttle=1.0, brake=1.0))
        time.sleep(4)

        # Collision history is a list callback is going to append to (we brake simulation on a collision)
        self.collision_hist = []
        self.lane_invasion_hist = []

        colsensor = self.world.get_blueprint_library().find('sensor.other.collision')
        lanesensor = self.world.get_blueprint_library().find('sensor.other.lane_invasion')
        self.colsensor = self.world.spawn_actor(colsensor, carla.Transform(), attach_to=self.vehicle)
        self.lanesensor = self.world.spawn_actor(lanesensor, carla.Transform(), attach_to=self.vehicle)
        self.colsensor.listen(self._collision_data)
        self.lanesensor.listen(self._lane_invasion_data)
        self.actor_list.append(self.colsensor)
        self.actor_list.append(self.lanesensor)

        self.world.tick()

        # Wait for a camera to send first image (important at the beginning of first episode)
        while self.front_image_Queue.empty():
            logging.debug("waiting for camera to be ready")
            time.sleep(0.01)
            self.world.tick()

        # Disengage brakes
        self.vehicle.apply_control(carla.VehicleControl(brake=0.0))

        image = self.front_image_Queue.get()
        image = np.array(image.raw_data)
        image = image.reshape((self.im_height, self.im_width, -1))
        image = image[:, :, :3]

        return image

    def step(self, action):
        total_reward = 0
        for _ in range(self.repeat_action):
            obs, rew, done, info = self._step(action)
            total_reward += rew
            if done:
                break
        return obs, total_reward, done, info

    # Steps environment
    def _step(self, action):
        self.world.tick()
        self.render()
            
        self.frame_step += 1

        # Apply control to the vehicle based on an action
        if self.action_type == 'continuous':
            action = carla.VehicleControl(throttle=float(action[0]), steer=float(action[1]))
            # if action[0] > 0:
            #     action = carla.VehicleControl(throttle=float(action[0]), steer=float(action[1]), brake=0)
            # else:
            #     action = carla.VehicleControl(throttle=0, steer=float(action[1]), brake= -float(action[0]))
        elif self.action_type == 'discrete':
            if action[0] == 0:
                action = carla.VehicleControl(throttle=0, steer=float((action[1] - 4)/4), brake=1)
            else:
                action = carla.VehicleControl(throttle=float((action[0])/3), steer=float((action[1] - 4)/4), brake=0)
        else:
            raise NotImplementedError()
        logging.debug('{}, {}, {}'.format(action.throttle, action.steer, action.brake))
        self.vehicle.apply_control(action)

        # Calculate speed in km/h from car's velocity (3D vector)
        v = self.vehicle.get_velocity()
        kmh = 3.6 * math.sqrt(v.x**2 + v.y**2 + v.z**2)

        loc = self.vehicle.get_location()

        #Calculate distant to end
        dist_to_end = loc.distance(self.end_transform.location)

    
        dist_text = str(dist_to_end)
        self.world.debug.draw_string(location=loc,text=dist_text,life_time=0.01)

        image = self.front_image_Queue.get()
        image = np.array(image.raw_data)
        image = image.reshape((self.im_height, self.im_width, -1))

        # TODO: Combine the sensors
        if 'rgb' in self.sensors:
            image = image[:, :, :3]
        if 'semantic' in self.sensors:
            image = image[:, :, 2]
            image = (np.arange(13) == image[..., None])
            image = np.concatenate((image[:, :, 2:3], image[:, :, 6:8]), axis=2)
            image = image * 255
            # logging.debug('{}'.format(image.shape))
            # assert image.shape[0] == self.im_height
            # assert image.shape[1] == self.im_width
            # assert image.shape[2] == 3

        # dis_to_left, dis_to_right, sin_diff, cos_diff = dist_to_roadline(self.map, self.vehicle)

        done = False
        reward = 0
        info = dict()

        
        if self.prev_dist > dist_to_end:
            reward += (self.prev_dist - dist_to_end) * 4
        else:
            reward += (self.prev_dist - dist_to_end) * 3

        if dist_to_end < 1.0:
            done = True
            reward += 1000
            self.prev_dist = dist_to_end

        # # If car collided - end and episode and send back a penalty
        if len(self.collision_hist) != 0:
            done = True
            reward += -120
            self.collision_hist = []
            self.lane_invasion_hist = []

        # 인도 침범에 대한 페널티 부여
        if 'sidewalk' in self.lane_invasion_hist:
        
            total_reward -= 50  # 인도 침범 시 50점 감점
            self.lane_invasion_hist.remove('sidewalk') 

        if len(self.lane_invasion_hist) != 0:
            reward += -10
            self.lane_invasion_hist = []

        # # Reward for speed
        # if not self.playing:
        #     reward += 0.1 * kmh * (self.frame_step + 1)
        # else:
        #     reward += 0.1 * kmh

        reward += 0.1 * kmh

        # reward += 1.3 * square_dist_diff

        # # Reward for distance to road lines
        # if not self.playing:
        #     reward -= math.exp(-dis_to_left)
        #     reward -= math.exp(-dis_to_right)
        
        if self.frame_step >= self.steps_per_episode:
            done = True


        #self.total_reward += reward

        self.world.debug.draw_arrow(begin=self.start_transform.location, end=self.end_transform.location, life_time=1.0)

        spectator = self.world.get_spectator()
        transform = self.vehicle.get_transform()
        spectator.set_transform(carla.Transform(transform.location + carla.Location(z=50),carla.Rotation(pitch=-90)))

        if done:
            # info['episode'] = {}
            # info['episode']['l'] = self.frame_step
            # info['episode']['r'] = reward
            logging.debug("Env lasts {} steps, restarting ... ".format(self.frame_step))
            self._destroy_agents()
        
        return image, reward, done, info
    
    def close(self):
        logging.info("Closes the CARLA server with process PID {}".format(self.server.pid))
        os.killpg(self.server.pid, signal.SIGKILL)
        atexit.unregister(lambda: os.killpg(self.server.pid, signal.SIGKILL))
    
    def render(self, mode='human'):
        # TODO: clean this
        # TODO: change the width and height to compat with the preview cam config

        if self.preview_camera_enabled:

            self._display, self._clock, self._font = graphics.setup(
                width=400,
                height=400,
                render=(mode=="human"),
            )

            preview_img = self.preview_image_Queue.get()
            preview_img = np.array(preview_img.raw_data)
            preview_img = preview_img.reshape((400, 400, -1))
            preview_img = preview_img[:, :, :3]
            graphics.make_dashboard(
                display=self._display,
                font=self._font,
                clock=self._clock,
                observations={"preview_camera":preview_img},
            )

            if mode == "human":
                # Update window display.
                pygame.display.flip()
            else:
                raise NotImplementedError()

    def _destroy_agents(self):

        for actor in self.actor_list:

            # If it has a callback attached, remove it first
            if hasattr(actor, 'is_listening') and actor.is_listening:
                actor.stop()

            # If it's still alive - desstroy it
            if actor.is_alive:
                actor.destroy()

        self.actor_list = []

    def _collision_data(self, event):

        # What we collided with and what was the impulse
        collision_actor_id = event.other_actor.type_id
        collision_impulse = math.sqrt(event.normal_impulse.x ** 2 + event.normal_impulse.y ** 2 + event.normal_impulse.z ** 2)

        # # Filter collisions
        # for actor_id, impulse in COLLISION_FILTER:
        #     if actor_id in collision_actor_id and (impulse == -1 or collision_impulse <= impulse):
        #         return

        # Add collision
        self.collision_hist.append(event)
    
    def _lane_invasion_data(self, event):
        # Change this function to filter lane invasions
        #self.lane_invasion_hist.append(event)
            # 차선 침범 이벤트에서 침범한 차선의 유형을 확인
        invasion_types = set([x.type for x in event.crossed_lane_markings])
        # 인도(Sidewalk) 침범을 확인
        if carla.LaneType.Sidewalk in invasion_types:
            # 인도 침범 시 보다 특정한 로직을 수행 (예: 리스트에 추가하는 대신 로그 남기기)
            logging.info("Sidewalk invasion detected!")
            # 인도 침범 시 별도로 처리할 내용이 있으면 여기에 추가
            self.lane_invasion_hist.append('sidewalk')  # 예시로 'sidewalk' 문자열 추가
        else:
            # 다른 유형의 차선 침범 처리
            self.lane_invasion_hist.extend(invasion_types)  # 기존 로직 유지


    def _on_highway(self):
        goal_abs_lane_id = 4
        vehicle_waypoint_closest_to_road = \
            self.map.get_waypoint(self.vehicle.get_location(), project_to_road=True, lane_type=carla.LaneType.Driving)
        road_id = vehicle_waypoint_closest_to_road.road_id
        lane_id_sign = int(np.sign(vehicle_waypoint_closest_to_road.lane_id))
        assert lane_id_sign in [-1, 1]
        goal_lane_id = goal_abs_lane_id * lane_id_sign
        vehicle_s = vehicle_waypoint_closest_to_road.s
        goal_waypoint = self.map.get_waypoint_xodr(road_id, goal_lane_id, vehicle_s)
        return not (goal_waypoint is None)

    def _get_start_transform(self):
        if self.start_transform_type == 'random':
            return random.choice(self.map.get_spawn_points())
        if self.start_transform_type == 'fixed':
            start_transform = self.map.get_spawn_points()[70]
            return start_transform
        if self.start_transform_type == 'highway':
            if self.map.name == "Town04":
                for trial in range(10):
                    start_transform = random.choice(self.map.get_spawn_points())
                    start_waypoint = self.map.get_waypoint(start_transform.location)
                    if start_waypoint.road_id in list(range(35, 50)): # TODO: change this
                        break
                return start_transform
            else:
                raise NotImplementedError
            
    def _get_end_tranform(self):
        indices = [213, 215, 217, 71, 221, 224, 72, 87, 108]
        end_transform = []
        for i in indices:
            end_transform.append(self.map.get_spawn_points()[i])
        return random.choice(end_transform)
