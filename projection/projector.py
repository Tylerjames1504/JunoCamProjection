import numpy as np
import json
from skimage import feature
from sklearn.metrics import pairwise_distances
from sklearn.neighbors import NearestNeighbors
import matplotlib.pyplot as plt
import spiceypy as spice
import tqdm
import multiprocessing
import time
import sys
from .globals import FRAME_HEIGHT, FRAME_WIDTH, initializer
from .cython_utils import furnish_c, project_midplane_c, get_pixel_from_coords_c
from .camera_funcs import CameraModel
from .spice_utils import get_kernels
from .frameletdata import FrameletData
import healpy as hp


class Projector:
    """
    The main projector class that determines the surface intercept points
    of each pixel in a JunoCam image

    Methods
    -------
    load_kernels: Determines and loads the required SPICE kernels
        for processing
    process_n_c : Projects an individual framelet in the JunoCam raw image
    process     : Driver for the projection that handles
        parallel processing
    """

    def __init__(self, imagefolder, meta, kerneldir="."):
        with open(meta, "r") as metafile:
            metadata = json.load(metafile)

        self.start_utc = metadata["START_TIME"]
        self.fname = metadata["FILE_NAME"].replace("-raw.png", "")

        print(f"Loading data for {self.fname}")

        # number of strips
        self.load_kernels(kerneldir)

        self.framedata = FrameletData(metadata, imagefolder)

        self.find_jitter(jitter_max=120)

    def load_kernels(self, KERNEL_DATAFOLDER):
        self.kernels = []
        kernels = get_kernels(KERNEL_DATAFOLDER, self.start_utc)
        for kernel in kernels:
            furnish_c(kernel.encode("ascii"))
            spice.furnsh(kernel)
            self.kernels.append(kernel)

    def find_jitter(self, jitter_max=25, plot=False):
        threshold = 0.1 * self.framedata.fullimg.max()

        for nci in range(self.framedata.nframes * 3):
            # find whether the planet limb is in now
            # approximating this as the first time the planet is seen
            # in the image, which is generally true..
            ci = nci % 3

            frame = self.framedata.framelets[nci].rawimg
            eti = self.framedata.framelets[nci].et

            if len(frame[frame > threshold]) > 5000:
                # make sure that the limb is also in the image
                # we want to find a frame where both the predicted limb
                # with no jitter and the actual limb are visible so that
                # the offset we determine is small
                cami = CameraModel(ci)
                limb_pts = self.get_limb(eti, cami)

                # bad test image if the limb is not fully visible
                # in the frame
                if len(limb_pts) > 5:
                    break

        # create the mask of the visible jupiter in the image
        imgmask = np.zeros_like(frame)
        imgmask[frame > threshold] = 1

        if plot:
            plt.figure(dpi=200)
            plt.imshow(frame, cmap="gray")
            plt.plot(limb_pts[:, 0], limb_pts[:, 1], "r-")
            plt.draw()
            plt.pause(0.001)

        # find the edges
        sigma = 8
        npoints = 0
        while npoints < 10:
            limb_img = np.asarray(
                feature.canny(frame[:, 24:1630], sigma=sigma), dtype=float
            )
            limb_img_points = np.dstack(np.where(limb_img == 1)[::-1])[0]
            limb_img_points[:, 0] += 24
            npoints = len(limb_img_points)
            sigma -= 1

        distances = pairwise_distances(limb_pts, limb_img_points)

        if plot:
            plt.plot(limb_img_points[:, 0], limb_img_points[:, 1], "g-")

        # we've found our limb. now we need to find
        # the actual limb from SPICE
        cami = CameraModel(ci)

        jitter_list = np.arange(-jitter_max, jitter_max, 1) / 1.0e3

        min_dists = np.zeros_like(jitter_list)

        for j, jitter in enumerate(tqdm.tqdm(jitter_list, desc="Finding jitter")):
            limbs_jcam = self.get_limb(eti + jitter, cami)

            if len(limbs_jcam) < 1:
                min_dists[j] = 1.0e10
                continue

            distances = pairwise_distances(limbs_jcam, limb_img_points)

            if plot:
                plt.figure(dpi=200)
                plt.imshow(frame, cmap="gray")
                plt.plot(
                    limbs_jcam[:, 0], limbs_jcam[:, 1], "r.", markersize=0.1
                )
                plt.plot(
                    limb_img_points[:, 0],
                    limb_img_points[:, 1],
                    "g.",
                    markersize=0.1,
                )
                plt.show()

            if len(distances[distances < 15] > 5):
                min_dists[j] = distances[distances < 15.0].mean()
            else:
                min_dists[j] = 1.0e10

        self.jitter = jitter_list[min_dists.argmin()]

        print("Found best jitter value of %.1f ms" % (self.jitter * 1.0e3))

        self.framedata.update_jitter(self.jitter)
        eti = self.framedata.framelets[nci].et

        limbs_jcam = self.get_limb(eti, cami)

        if plot:
            plt.plot(limbs_jcam[:, 0], limbs_jcam[:, 1], "g.", markersize=0.1)
            plt.show()

    def get_limb(self, eti, cami):
        METHOD = "TANGENT/ELLIPSOID"
        CORLOC = "ELLIPSOID LIMB"

        scloc, lt = spice.spkpos("JUNO", eti, "IAU_JUPITER", "CN+S", "JUPITER")

        # use the sub spacecraft point as the reference vector
        refvec, _, _ = spice.subpnt("INTERCEPT/ELLIPSOID", "JUPITER", eti, "IAU_JUPITER", "CN+S", "JUNO")

        _, _, eplimbs, limbs_IAU = spice.limbpt(METHOD, "JUPITER", eti, "IAU_JUPITER", "CN+S",
                                                CORLOC, "JUNO", refvec, np.radians(0.1), 3600,
                                                4.0, 0.001, 7200)

        limbs_jcam = np.zeros((limbs_IAU.shape[0], 2))
        for i, limbi in enumerate(limbs_IAU):
            transform = spice.pxfrm2(
                "IAU_JUPITER", "JUNO_JUNOCAM", eplimbs[i], eti
            )
            veci = np.matmul(transform, limbs_IAU[i, :])
            limbs_jcam[i, :] = cami.vec2pix(veci)

        mask = (
            (limbs_jcam[:, 1] < 128)
            & (limbs_jcam[:, 1] > 0)
            & (limbs_jcam[:, 0] > 0)
            & (limbs_jcam[:, 0] < 1648)
        )
        limbs_jcam = limbs_jcam[mask, :]

        return limbs_jcam

    def process(self, nside=512, num_procs=8, apply_LS=True, n_neighbor=5):
        print(f"Projecting {self.fname} to a HEALPix grid with n_side={nside}")

        self.framedata.get_backplane(num_procs)

        if apply_LS:
            self.framedata.update_image(
                apply_lommel_seeliger(self.framedata.image, self.framedata.incidence, self.framedata.emission)
            )

        coords_new = np.transpose(self.framedata.coords, (1, 0, 2, 3, 4)).reshape(3, -1, 2)
        imgvals_new = np.transpose(self.framedata.image, (1, 0, 2, 3)).reshape(3, -1)

        map = self.project_to_healpix(nside, coords_new, imgvals_new, n_neighbor=n_neighbor)

        return map

    def project_to_healpix(self, nside, coords, imgvals, n_neighbor=4):
        # get the image extents in pixel coordinate space
        # clip half a pixel to avoid edge artifacts
        x0 = np.nanmin(coords[:, :, 0]) + 0.5
        x1 = np.nanmax(coords[:, :, 0]) - 0.5
        y0 = np.nanmin(coords[:, :, 1]) + 0.5
        y1 = np.nanmax(coords[:, :, 1]) - 0.5

        extents = np.array([x0, x1, y0, y1])

        # now we construct the map grid
        longrid, latgrid = hp.pix2ang(nside, list(range(hp.nside2npix(nside))), lonlat=True)

        pix = np.nan * np.zeros((longrid.size, 2))

        # this assumes the midplane mode where the coordinates
        # are using the Green band camera
        et = self.framedata.tmid

        # get the spacecraft location and transformation to and from the JUNOCAM coordinates
        get_pixel_from_coords_c(np.radians(longrid), np.radians(latgrid), longrid.size, et, extents, pix)

        # get the locations that JunoCam observed
        inds = np.where(np.isfinite(pix[:, 0] * pix[:, 1]))[0]
        pix = pix[inds]
        pixel_inds = hp.ang2pix(nside, longrid[inds], latgrid[inds], lonlat=True)

        # finally, project the image onto the healpix grid
        m = create_image_from_grid(coords, imgvals, pix, pixel_inds, longrid.shape, n_neighbor=n_neighbor)

        return m


def apply_lommel_seeliger(imgvals, incidence, emission):
    '''
        Apply the Lommel-Seeliger correction for incidence
    '''
    print("Applying Lommel-Seeliger correction")
    # apply Lommel-Seeliger correction
    mu0 = np.cos(incidence)
    mu = np.cos(emission)
    corr = 1. / (mu + mu0)
    corr[np.abs(incidence) > np.radians(89)] = np.nan
    imgvals = imgvals * corr

    return imgvals


def create_image_from_grid(coords, imgvals, pix, inds, img_shape, n_neighbor=5, min_dist=25.):
    '''
        Reproject an irregular spaced image onto a regular grid from a list of coordinate
        locations and corresponding image values. This uses an inverse lookup-table defined
        by `pix`, where pix gives the coordinates in the original image where the corresponding
        pixel coordinate on the new image should be. The coordinate on the new image is given by
        the inds variable.
    '''
    # break up the image and coordinate data into the different filters
    Rcoords = coords[2, :]
    Gcoords = coords[1, :]
    Bcoords = coords[0, :]

    Rmask = np.isfinite(Rcoords[:, 0] * Rcoords[:, 1])
    Gmask = np.isfinite(Gcoords[:, 0] * Gcoords[:, 1])
    Bmask = np.isfinite(Bcoords[:, 0] * Bcoords[:, 1])

    Rcoords = Rcoords[Rmask]
    Gcoords = Gcoords[Gmask]
    Bcoords = Bcoords[Bmask]

    Rimg = imgvals[2, Rmask]
    Gimg = imgvals[1, Gmask]
    Bimg = imgvals[0, Bmask]

    # fit the nearest neighbour algorithm
    print("Fitting neighbors")
    Rneigh = NearestNeighbors().fit(Rcoords)
    Gneigh = NearestNeighbors().fit(Gcoords)
    Bneigh = NearestNeighbors().fit(Bcoords)

    # get the corresponding pixel coordinates in the original image for each filter
    print("Finding neighbors for new image")
    R_dist, R_ind = Rneigh.kneighbors(pix, n_neighbor)
    G_dist, G_ind = Gneigh.kneighbors(pix, n_neighbor)
    B_dist, B_ind = Bneigh.kneighbors(pix, n_neighbor)

    # convert the distance to weights. the farther the point from the
    # target pixel, the less weight it has in the final average
    # add a small epsilon if we accidently are on the exact same point
    R_wght = 1. / (R_dist + 1.e-16)
    G_wght = 1. / (G_dist + 1.e-16)
    B_wght = 1. / (B_dist + 1.e-16)

    # normalize the weights
    R_wght = R_wght / np.sum(R_wght, axis=1, keepdims=True)
    G_wght = G_wght / np.sum(G_wght, axis=1, keepdims=True)
    B_wght = B_wght / np.sum(B_wght, axis=1, keepdims=True)

    R_wght[R_dist.min(axis=1) > min_dist] = 0.
    G_wght[G_dist.min(axis=1) > min_dist] = 0.
    B_wght[B_dist.min(axis=1) > min_dist] = 0.

    # get the weighted NN average for each pixel
    print("Calculating image values at new locations")
    R_vals = np.sum(np.take(Rimg, R_ind, axis=0) * R_wght, axis=1)
    G_vals = np.sum(np.take(Gimg, G_ind, axis=0) * G_wght, axis=1)
    B_vals = np.sum(np.take(Bimg, B_ind, axis=0) * B_wght, axis=1)

    IMG = np.zeros((*img_shape, 3))
    # loop through each point observed by JunoCam and assign the pixel value
    for k, ind in enumerate(tqdm.tqdm(inds, desc='Building image')):
        if len(img_shape) == 2:
            j, i = np.unravel_index(ind, img_shape)

            # do the weighted average for each filter
            IMG[j, i, 0] = R_vals[k]
            IMG[j, i, 1] = G_vals[k]
            IMG[j, i, 2] = B_vals[k]
        else:
            IMG[ind, 0] = R_vals[k]
            IMG[ind, 1] = G_vals[k]
            IMG[ind, 2] = B_vals[k]

    IMG[~np.isfinite(IMG)] = 0.

    return IMG
