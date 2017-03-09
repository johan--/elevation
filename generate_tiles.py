#!/usr/bin/env python3

from subprocess import call
import sys, os, os.path
from queue import Queue
from argparse import ArgumentParser
import time
import errno
import threading
import datetime
import errno
import multiprocessing
import queue
from random import randint, random
from os import getpid

import map_utils

try:
    import mapnik2 as mapnik
except:
    import mapnik

import logging
from logging import debug, info
log_format= "%(asctime)s %(name)16s:%(lineno)-4d (%(funcName)-21s) %(levelname)-8s %(message)s"


try:
    NUM_CPUS = multiprocessing.cpu_count()
except NotImplementedError:
    NUM_CPUS = 1


def floor(i, base=1):
    """Round down i to the closest multiple of base."""
    return base * (i // base)


class RenderStack:
    """A render stack implemented with a list.
    Although this is implemented with a list, I prefer the semantic of these
    methods and the str() representation given by the list being pop from/push
    into the left.

    The stack has a first element, which is the one ready to be pop()'ed.
    Because this element might need to be returned, there's the confirm()
    method which actually pops it and replaces it with the next one.

    Finally, the stack autofills with children when we pop an element."""
    def __init__(self, max_zoom, metatile_size):
        # I don't need order here, it's (probably) better if I validate tiles
        # as soon as possible
        self.first = None
        self.ready = []
        self.to_validate = set()
        self.max_zoom = max_zoom
        self.metatile_size = metatile_size


    def push(self, o):
        self.to_validate.add(o)
        debug("%s, %s, %s", self.first, self.ready, self.to_validate)


    def pop(self):
        return self.first


    def confirm(self):
        if self.first is not None:
            z, x, y = self.first
            if z < self.max_zoom:
                for r, c in ( (0, 0),                  (0, self.metatile_size),
                              (self.metatile_size, 0), (self.metatile_size, self.metatile_size) ):
                    new_work = (z+1, x*2+r, y*2+c)
                    self.push(new_work)

        if len(self.ready) > 0:
            t = self.ready.pop(0)
        else:
            t = None

        self.first = t
        debug("%s, %s, %s", self.first, self.ready, self.to_validate)


    def size(self):
        # HACK: int(bool) \belongs (0, 1)
        # debug("%s, %s, %s", self.first, self.ready, self.to_validate)
        ans = ( int(self.first is not None) + len(self.ready) +
                len(self.to_validate) )
        # debug(ans)
        return ans


    def notify(self, data):
        tile, render = data
        if tile[0] <= self.max_zoom:
            self.to_validate.remove(tile)

            if render:
                if self.first is not None:
                    self.ready.insert(0, self.first)

                self.first = tile


class RenderThread:
    def __init__(self, opts, backend, queues):
        self.backend = backend
        self.queues  = queues
        self.opts = opts
        self.metatile_size = opts.metatile_size
        self.tile_size = 256
        self.image_size = self.tile_size*self.metatile_size
        start = time.perf_counter()
        self.m  = mapnik.Map(self.image_size, self.image_size)
        # Load style XML
        if not self.opts.dry_run:
            mapnik.load_map(self.m, opts.mapfile, True)
        end = time.perf_counter()
        debug('Map loading took %.6fs', end-start)
        # Obtain <Map> projection
        self.prj = mapnik.Projection(self.m.srs)
        # Projects between tile pixel co-ordinates and LatLong (EPSG:4326)
        self.tileproj = map_utils.GoogleProjection(opts.max_zoom+1)


    def render_tile(self, z, x, y):
        # Calculate pixel positions of bottom-left & top-right
        p0 = (x * self.tile_size, (y + self.metatile_size) * self.tile_size)
        p1 = ((x + self.metatile_size) * self.tile_size, y * self.tile_size)

        # Convert to LatLong (EPSG:4326)
        l0 = self.tileproj.fromPixelToLL(p0, z);
        l1 = self.tileproj.fromPixelToLL(p1, z);

        # Convert to map projection (e.g. mercator co-ords EPSG:900913)
        c0 = self.prj.forward(mapnik.Coord(l0[0], l0[1]))
        c1 = self.prj.forward(mapnik.Coord(l1[0], l1[1]))

        # Bounding box for the tile
        if hasattr(mapnik, 'mapnik_version') and mapnik.mapnik_version() >= 800:
            bbox = mapnik.Box2d(c0.x, c0.y, c1.x, c1.y)
        else:
            bbox = mapnik.Envelope(c0.x, c0.y, c1.x, c1.y)

        self.m.resize(self.image_size, self.image_size)
        self.m.zoom_to_box(bbox)
        if self.m.buffer_size < 128:
            self.m.buffer_size = 128

        # we must decide wether to render the subtiles/children of this tile
        # for that we keep this map of children coords to a bool
        semi_metatile_size = max(self.metatile_size // 2, 1)
        render_children = {}
        for i in (0, semi_metatile_size):
            render_children[i] = {}
            for j in (0, semi_metatile_size):
                render_children[i][j] = False

        if not self.opts.dry_run:
            # Render image with default Agg renderer
            start = time.perf_counter()
            im = mapnik.Image(self.image_size, self.image_size)

            try:
                mapnik.render(self.m, im)
            except RuntimeError as e:
                print("%d:%d:%d: %s" % (z, x, y, e), file=sys.stderr)
                sys.stderr.flush()
            else:
                mid= time.perf_counter()

                # save the image, splitting it in the right amount of tiles
                # we use min() so we can support low zoom levels with less than
                # metatile_size tiles
                tiles = min(self.metatile_size, 2**z)
                for i in range(tiles):
                    for j in range(tiles):
                        img = im.view(i*self.tile_size, j*self.tile_size, self.tile_size, self.tile_size)
                        data = img.tostring('png256')
                        is_empty = map_utils.is_empty(data)

                        if not is_empty:
                            self.backend.store(z, x+i, y+j, data)

                            child_i = floor(i, tiles // 2)
                            child_j = floor(j, tiles // 2)
                            # at least something to render. note that if we're
                            # rendering only one tile (either metatile_size == 1
                            # or z == 0), i, j can only be == 0. this matches
                            # the situation further down
                            render_children[child_i][child_j] = True
                        else:
                            if self.opts.empty == 'skip':
                                # empty tile, skip
                                debug("%d:%d:%d: empty" % (z, x+i, y+j))
                                continue
                            # TODO: else?

                    self.backend.commit()

                end = time.perf_counter()
                info("%d:%d:%d: %f, %f" % (z, x, y, mid-start, end-mid))

        else:
            # simulate some work
            time.sleep(randint(0, 30) / 10)
            for i in (0, semi_metatile_size):
                for j in (0, semi_metatile_size):
                    if random() <= 0.75:
                        render_children[i][j] = True

        debug(render_children)
        for i in (0, self.opts.metatile_size):
            for j in (0, self.opts.metatile_size):
                # TODO: do not do it blindly
                t = (z+1, x*2+i, y*2+j)
                debug("<== [%s] %r" % (getpid(), t, ))
                # render_children is indexed in semi_metatile_size and i, j
                # are either 0 or metatile_size, that's why we // 2 here
                # note that if metatile_size == 1, i, j can only be == 0
                self.queues[1].put((t, render_children[i // 2][j // 2]))
                debug("<<< [%s]" % getpid())


    def loop(self):
        debug('%s looping the loop', self)
        while True:
            # Fetch a tile from the queue and render it
            r = self.queues[0].get()
            debug("[%s] ==> %r" % (getpid(), r, ))
            if r is None:
                # self.q.task_done()
                debug('[%s] ending loop' % getpid())
                break
            else:
                (z, x, y) = r

            if self.opts.skip_existing or self.opts.skip_newer is not None:
                debug('skip test existing:%s, newer:%s',
                       self.opts.skip_existing, self.opts.skip_newer)
                skip= True
                # we use min() so we can support low zoom levels with less than metatile_size tiles
                for tile_x in range(x, x+min(self.metatile_size, 2**z)):
                    for tile_y in range(y, y+min(self.metatile_size, 2**z)):
                        if self.opts.skip_existing:
                            skip= skip and self.backend.exists(z, tile_x, tile_y)
                        else:
                            skip=(skip and
                                self.backend.newer_than(z, tile_x, tile_y,
                                                         self.opts.skip_newer))
            else:
                skip= False

            if not skip:
                self.render_tile(z, x, y)
            else:
                if self.opts.skip_existing:
                    print("%d:%d:%d: present, skipping" % (z, x, y))
                else:
                    print("%d:%d:%d: too new, skipping" % (z, x, y))

            # self.q.task_done()


class Master:
    def __init__(self, opts):
        self.opts = opts
        # we need at least space for the initial batch
        self.renderers = {}
        self.work_stack = RenderStack(opts.max_zoom, opts.metatile_size)

        if self.opts.parallel == 'fork':
            debug('forks, using mp.Queue()')

            self.queues = (multiprocessing.Queue(  self.opts.threads + 1),
                           multiprocessing.Queue(4*self.opts.threads + 1))
        else:
            debug('threads or single, using queue.Queue()')
            # TODO: this and the warning about mapnik and multithreads
            self.queues = (Queue(32), None)


    def render_tiles(self):
        debug("render_tiles(%s)", self.opts)

        backends = dict(
            tiles=  map_utils.DiskBackend,
            mbtiles=map_utils.MBTilesBackend,
            )

        try:
            backend = backends[self.opts.format](self.opts.tile_dir, self.opts.bbox)
        except KeyError:
            raise

        # Launch rendering threads
        for i in range(self.opts.threads):
            renderer = RenderThread(self.opts, backend, self.queues)

            if self.opts.parallel!='single':
                if self.opts.parallel == 'fork':
                    debug('mp.Process()')
                    render_thread = multiprocessing.Process(target=renderer.loop)
                elif self.opts.parallel == 'threads':
                    debug('th.Thread()')
                    render_thread = threading.Thread(target=renderer.loop)

                render_thread.start()

                if self.opts.parallel:
                    debug("Started render thread %s" % render_thread.name)
                else:
                    debug("Started render thread %s" % render_thread.getName())

                self.renderers[i] = render_thread

        if not os.path.isdir(self.opts.tile_dir):
            debug("creating dir %s", self.opts.tile_dir)
            os.makedirs(self.opts.tile_dir, exist_ok=True)

        if self.opts.tiles is None:
            debug('rendering bbox %s:%s', self.opts.bbox_name, self.opts.bbox)
            self.render_bbox()
        else:
            # TODO: if possible, order them in depth first/proximity? fashion.
            debug('rendering individual tiles')
            for i in self.opts.tiles:
                z, x, y = map(int, i.split(','))
                self.queues[0].put((z, x, y))
                # TODO: either pop from work_in or add param to not render children

        if self.opts.parallel == 'single':
            self.queues[0].put(None)
            renderer.loop()

        self.finish()


    def render_bbox(self):
        start = time.time()
        work_out, work_in = self.queues
        # for each tile that was sent to be worked on, 4 should return
        # this will be important later on
        went_out, came_back = 0, 0

        gprj = map_utils.GoogleProjection(self.opts.max_zoom+1)

        bbox  = self.opts.bbox
        ll0=(bbox[0], bbox[3])
        ll1=(bbox[2], bbox[1])

        image_size = 256.0*self.opts.metatile_size

        # we start by adding the min_zoom tiles and let the system handle the rest
        px0 = gprj.fromLLtoPixel(ll0, self.opts.min_zoom)
        px1 = gprj.fromLLtoPixel(ll1, self.opts.min_zoom)

        for x in range(int(px0[0]/image_size), int(px1[0]/image_size)+1):
            # Validate x co-ordinate
            if ((x < 0) or
                (x*self.opts.metatile_size >= 2**self.opts.min_zoom)):

                continue

            for y in range(int(px0[1]/image_size), int(px1[1]/image_size)+1):
                # Validate x co-ordinate
                if ((y < 0) or
                    (y*self.opts.metatile_size >= 2**self.opts.min_zoom)):

                    continue

                # Submit tile to be rendered into the queue
                t = (self.opts.min_zoom, x*self.opts.metatile_size,
                     y*self.opts.metatile_size)
                debug("... %r" % (t, ))
                self.work_stack.push(t)
                # make sure they're rendered!
                self.work_stack.notify((t, True))

        # I wish I could get to the underlying pipes so I could select() on them
        # NOTE: work_out._writer, self.queues[1]._reader
        while self.work_stack.size() > 0:
            try:
                # we have space to put things,
                # pop from the reader,
                while True:
                    try:
                        data = work_in.get(True, .1)
                    except queue.Empty:
                        # debug('in: timeout!')
                        break
                    else:
                        debug("<-- %r" % (data, ))
                        self.work_stack.notify(data)
                        came_back += 1

                while True:
                    try:
                        # pop from there,
                        new_work = self.work_stack.pop()
                    except IndexError:
                        debug('out: timeout!')
                        break
                    else:
                        if new_work is not None:
                            try:
                                # push in the writer
                                debug("--> %r" % (new_work, ))
                                work_out.put(new_work, True, .1)
                                self.work_stack.confirm()
                                went_out += 1
                            except queue.Full:
                                debug('work_out full, not confirm()ing.')
                                break
                        else:
                            break

            except KeyboardInterrupt as e:
                debug(e)
                self.finish()
                raise SystemExit("Ctrl-c detected, exiting...")

        while went_out*4 > came_back:
            debug("%d <-> %d", went_out*4, came_back)
            data = work_in.get(True)
            debug("<-- %r", data)
            came_back += 1

        debug('out!')


    def finish(self):
        if self.opts.parallel!='single':
            debug('finishing threads/procs')
            # Signal render threads to exit by sending empty request to queue
            for i in range(self.opts.threads):
                debug("--> None")
                self.queues[0].put(None)

            # wait for pending rendering jobs to complete
            if not self.opts.parallel == 'fork':
                self.queues[0].join()
            else:
                self.queues[0].close()
                self.queues[0].join_thread()

            for i in range(self.opts.threads):
                self.renderers[i].join()


def parse_args():
    parser = ArgumentParser()

    parser.add_argument('-b', '--bbox',          dest='bbox',      default=[-180, -85, 180, 85], type=map_utils.bbox)
    parser.add_argument('-B', '--bbox-name',     dest='bbox_name', default=None)
    parser.add_argument('-n', '--min-zoom',      dest='min_zoom',  default=0, type=int)
    parser.add_argument('-x', '--max-zoom',      dest='max_zoom',  default=18, type=int)

    parser.add_argument(      '--tiles',         dest='tiles',     default= None, nargs='+', metavar='Z,X,Y')

    parser.add_argument('-i', '--input-file',    dest='mapfile',   default='osm.xml')
    parser.add_argument('-f', '--format',        dest='format',    default='tiles') # also 'mbtiles'
    parser.add_argument('-o', '--output-dir',    dest='tile_dir',  default='tiles/')

    # TODO: check it's a power of 2
    parser.add_argument('-m', '--metatile-size', dest='metatile_size', default=1, type=int)

    parser.add_argument('-t', '--threads',       dest='threads',   default=NUM_CPUS, type=int)
    parser.add_argument('-p', '--parallel-method', dest='parallel', default='fork', choices=('threads', 'fork', 'single'))

    parser.add_argument('-X', '--skip-existing', dest='skip_existing', default=False, action='store_true')
    parser.add_argument('-N', '--skip-newer',    dest='skip_newer', default=None, type=int, metavar='DAYS')
    # parser.add_argument('-L', '--skip-symlinks', dest='skip_', default=None, type=int)
    parser.add_argument('-E', '--empty',         dest='empty',     default='skip', choices=('skip', 'link', 'render'))

    parser.add_argument('-d', '--debug',         dest='debug',     default=False, action='store_true')
    parser.add_argument(      '--dry-run',       dest='dry_run',   default=False, action='store_true')
    # TODO: buffer size (256?)
    opts = parser.parse_args()

    if opts.debug:
        logging.basicConfig(level=logging.DEBUG, format=log_format)
    else:
        logging.basicConfig(format=log_format)

    if opts.format == 'tiles' and opts.tile_dir[-1]!='/':
        # we need the trailing /, it's actually a series of BUG s in render_tiles()
        opts.tile_dir += '/'

    opts.tile_dir = os.path.abspath(opts.tile_dir)
    if opts.skip_newer is not None:
        opts.skip_newer = datetime.datetime.now()-datetime.timedelta(days=opts.skip_newer)

    # so we find any relative resources
    # os.chdir(os.path.dirname(opts.mapfile))
    opts.mapfile = os.path.basename(opts.mapfile)

    # pick bbox from bboxes.ini
    if opts.bbox_name is not None:
        a = map_utils.Atlas([ opts.bbox_name ])
        opts.bbox = a.maps[opts.bbox_name].bbox

    if opts.parallel == 'single':
        opts.threads = 1

    return opts


if __name__  ==  "__main__":
    opts = parse_args()

    master = Master(opts)
    master.render_tiles()
