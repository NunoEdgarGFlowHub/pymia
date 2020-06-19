import abc
import pickle
import typing

import numpy as np

import pymia.data.definition as defs
import pymia.data.transformation as tfm


class Assembler(abc.ABC):
    """Interface for assembling images from batch, which contain parts (chunks) of the images only."""

    @abc.abstractmethod
    def add_batch(self, to_assemble, batch: dict, last_batch=False):
        """

        Args:
            to_assemble (object): object(s) to be assembled to an image.
            batch (dict): The information of a batch.
            last_batch (bool): Whether the current batch is the last.
        """
        pass

    @abc.abstractmethod
    def get_assembled_subject(self, subject_index: int):
        """
        Args:
            subject_index (int): Index of the assembled subject to be retrieved.

        Returns:
            object: The assembled data of the subject (might be multiple arrays).
        """
        pass

    @property
    @abc.abstractmethod
    def subjects_ready(self):
        """
        Returns:
            list, set: The indices of the subjects that are finished assembling.

        """
        pass


def numpy_zeros(shape: tuple, id_: str, batch: dict, idx: int):
    return np.zeros(shape)


def default_sample_fn(params: dict):
    key = params['key']
    batch = params['batch']
    idx = params['batch_idx']

    data = params[key]
    index_expr = batch[defs.KEY_INDEX_EXPR][idx]
    if isinstance(index_expr, bytes):
        # is pickled
        index_expr = pickle.loads(index_expr)

    return data, index_expr


class SubjectAssembler(Assembler):

    def __init__(self, zero_fn=numpy_zeros, on_sample_fn=default_sample_fn):
        """Assembles predictions of one or multiple subjects.

        Assumes that the network output, i.e. to_assemble, is of shape (B, ..., C)
        where B is the batch size and C is the numbers of channels (must be at least 1) and ... refers to an arbitrary image
        dimension.

        Args:
            zero_fn: A function that initializes the numpy array to hold the predictions.
                Args: shape: tuple with the shape of the subject's labels, id: str identifying the subject.
                Returns: A np.ndarray
            on_sample_fn: A function that processes a sample.
                Args: params: dict with the parameters.
                Returns tuple of data and index expression, i.e. the processed data and index expression.
        """
        self.predictions = {}
        self._subjects_ready = set()
        self.zero_fn = zero_fn
        self.on_sample_fn = on_sample_fn

    @property
    def subjects_ready(self):
        """see :meth:`Assembler.subjects_ready`"""
        return self._subjects_ready

    def add_batch(self, to_assemble, batch: dict, last_batch=False):
        """see :meth:`Assembler.add_batch`"""
        if defs.KEY_SUBJECT_INDEX not in batch:
            raise ValueError('SubjectAssembler requires "subject_index" to be extracted (use IndexingExtractor)')
        if defs.KEY_INDEX_EXPR not in batch:
            raise ValueError('SubjectAssembler requires "index_expr" to be extracted (use IndexingExtractor)')
        if defs.KEY_SHAPE not in batch:
            raise ValueError('SubjectAssembler requires "shape" to be extracted (use ImageShapeExtractor)')

        if not isinstance(to_assemble, dict):
            to_assemble = {'__prediction': to_assemble}

        for idx in range(len(batch[defs.KEY_SUBJECT_INDEX])):
            self.add_sample(to_assemble, batch, idx)

        if last_batch:
            # to prevent from last batch to be ignored
            self.end()

    def end(self):
        self._subjects_ready = set(self.predictions.keys())

    def add_sample(self, to_assemble, batch, idx):

        subject_index = batch[defs.KEY_SUBJECT_INDEX][idx]

        # initialize subject
        if subject_index not in self.predictions and not self.predictions:
            self.predictions[subject_index] = self._init_new_subject(batch, to_assemble, idx)
        elif subject_index not in self.predictions:
            self._subjects_ready = set(self.predictions.keys())
            self.predictions[subject_index] = self._init_new_subject(batch, to_assemble, idx)

        for key in to_assemble:
            data = to_assemble[key][idx]
            # todo: maybe replace the dict parameters by actual arguments. Interface does not need to be very generic.
            sample_data, index_expr = self.on_sample_fn({'key': key,
                                                         key: data,
                                                         'batch': batch,
                                                         'batch_idx': idx,
                                                         'predictions': self.predictions})

            self.predictions[subject_index][key][index_expr.expression] = sample_data

    def _init_new_subject(self, batch, to_assemble, idx):
        subject_prediction = {}
        for key in to_assemble:
            subject_shape = batch[defs.KEY_SHAPE][idx]
            subject_shape += (to_assemble[key].shape[-1],)
            subject_prediction[key] = self.zero_fn(subject_shape, key, batch, idx)
        return subject_prediction

    def get_assembled_subject(self, subject_index: int):
        """see :meth:`Assembler.get_assembled_subject`"""
        try:
            self._subjects_ready.remove(subject_index)
        except KeyError:
            # check if subject is assembled but not listed as ready
            # this can happen if only one subject was assembled or last
            if subject_index not in self.predictions:
                raise ValueError('Subject with index {} not in assembler'.format(subject_index))
        assembled = self.predictions.pop(subject_index)
        if '__prediction' in assembled:
            return assembled['__prediction']
        return assembled


def mean_merge_fn(planes: list):
    return np.stack(planes).mean(axis=0)


class TransformSampleFn:

    def __init__(self, transform: tfm.Transform) -> None:
        self.transform = transform

    def __call__(self, params: dict):
        key = params['key']
        batch = params['batch']
        idx = params['batch_idx']

        index_expr = batch[defs.KEY_INDEX_EXPR][idx]
        if isinstance(index_expr, bytes):
            # is pickled
            index_expr = pickle.loads(index_expr)

        temp = tfm.raise_error_if_entry_not_extracted
        tfm.raise_error_entry_not_extracted = False
        ret = self.transform({key: params[key], defs.KEY_INDEX_EXPR: index_expr})
        tfm.raise_error_entry_not_extracted = temp

        return ret[key], ret[defs.KEY_INDEX_EXPR]


class PlaneSubjectAssembler(Assembler):
    def __init__(self, merge_fn=mean_merge_fn, zero_fn=numpy_zeros):
        """Assembles predictions of one or multiple subjects where predictions are made in all three planes.

        This class assembles the prediction from all planes (axial, coronal, sagittal) and merges the prediction
        according to :code:`merge_fn`.

        Assumes that the network output, i.e. to_assemble, is of shape (B, ..., C)
        where B is the batch size and C is the numbers of channels (must be at least 1) and ... refers to an arbitrary image
        dimension.

        Args:
            merge_fn: A function that processes a sample.
                Args: planes: list with the assembled prediction for all planes.
                Returns: Merged numpy.ndarray
            zero_fn: A function that initializes the numpy array to hold the predictions.
                Args: shape: tuple with the shape of the subject's labels, id: str identifying the subject.
                Returns: A np.ndarray
        """

        self.planes = {}  # type: typing.Dict[int, SubjectAssembler]
        self._subjects_ready = set()
        self.zero_fn = zero_fn
        self.merge_fn = merge_fn

    @property
    def subjects_ready(self):
        """see :meth:`Assembler.subjects_ready`"""
        return self._subjects_ready

    def add_batch(self, to_assemble, batch: dict, last_batch=False):
        """see :meth:`Assembler.add_batch`"""
        if defs.KEY_INDEX_EXPR not in batch:
            raise ValueError('SubjectAssembler requires "index_expr" to be extracted (use IndexingExtractor)')
        if defs.KEY_SHAPE not in batch:
            raise ValueError('SubjectAssembler requires "shape" to be extracted (use ImageShapeExtractor)')

        if not isinstance(to_assemble, dict):
            to_assemble = {'__prediction': to_assemble}

        for idx in range(len(batch[defs.KEY_INDEX_EXPR])):
            index_expr = batch[defs.KEY_INDEX_EXPR][idx]
            if isinstance(index_expr, bytes):
                # is pickled
                index_expr = pickle.loads(index_expr)
            plane_dimension = self._get_plane_dimension(index_expr)

            if plane_dimension not in self.planes:
                self.planes[plane_dimension] = SubjectAssembler(self.zero_fn)

            indexing = index_expr.get_indexing()
            if not isinstance(indexing, list):
                indexing = [indexing]

            required_plane_shape = list(batch[defs.KEY_SHAPE][idx])

            index_at_plane = indexing[plane_dimension]
            if isinstance(index_at_plane, tuple):
                # is a range in the off plane direction (tuple)
                required_off_plane_size = index_at_plane[1] - index_at_plane[0]
                required_plane_shape[plane_dimension] = required_off_plane_size
            else:  # isinstance of int
                # is one slice in off plane direction (int)
                required_plane_shape.pop(plane_dimension)
            transform = tfm.SizeCorrection(tuple(required_plane_shape), entries=tuple(to_assemble.keys()))
            self.planes[plane_dimension].on_sample_fn = TransformSampleFn(transform)

            self.planes[plane_dimension].add_sample(to_assemble, batch, idx)

        ready = None
        for plane_assembler in self.planes.values():
            if last_batch:
                plane_assembler.end()

            if ready is None:
                ready = set(plane_assembler.subjects_ready)
            else:
                ready.intersection_update(plane_assembler.subjects_ready)
        self._subjects_ready = ready

    def get_assembled_subject(self, subject_index: int):
        """see :meth:`Assembler.get_assembled_subject`"""
        try:
            self._subjects_ready.remove(subject_index)
        except KeyError:
            # check if subject is assembled but not listed as ready
            # this can happen if only one subject was assembled or last
            if subject_index not in self.planes[0].predictions:
                raise ValueError('Subject with index {} not in assembler'.format(subject_index))

        assembled = {}
        for plane in self.planes.values():
            ret_val = plane.get_assembled_subject(subject_index)
            if not isinstance(ret_val, dict):
                ret_val = {'__prediction': ret_val}
            for key, value in ret_val.items():
                assembled.setdefault(key, []).append(value)

        for key in assembled:
            assembled[key] = self.merge_fn(assembled[key])

        if '__prediction' in assembled:
            return assembled['__prediction']
        return assembled

    @staticmethod
    def _get_plane_dimension(index_expr):
        for i, entry in enumerate(index_expr.expression):
            if isinstance(entry, int):
                return i
            if isinstance(entry, slice) and entry != slice(None):
                return i


class Subject2dAssembler(Assembler):

    def __init__(self, id_entry='ids') -> None:
        """Assembles predictions of two-dimensional images.

        Two-dimensional images do not specifically require assembling. For pipeline compatibility reasons this class provides
        , nevertheless, a implementation for the two-dimensional case.

        Args:
            id_entry (str): Entry that should contain the subject index in :code:`batch`
        """
        super().__init__()
        self._subjects_ready = set()
        self.predictions = {}
        self.id_entry = id_entry

    @property
    def subjects_ready(self):
        """see :meth:`Assembler.subjects_ready`"""
        return self._subjects_ready

    def add_batch(self, to_assemble, batch: dict, last_batch=False):
        """see :meth:`Assembler.add_batch`"""
        if self.id_entry not in batch:
            raise ValueError('Subject2dAssembler requires "{}" to be in the batch'.format(self.id_entry))

        if not isinstance(to_assemble, dict):
            to_assemble = {'__prediction': to_assemble}

        for idx in range(len(batch[self.id_entry])):
            subject_index = batch[self.id_entry][idx]
            for key in to_assemble:
                self.predictions.setdefault(subject_index, {})[key] = to_assemble[key][idx]
                self._subjects_ready.add(subject_index)

    def get_assembled_subject(self, subject_index):
        """see :meth:`Assembler.get_assembled_subject`"""
        try:
            self._subjects_ready.remove(subject_index)
        except KeyError:
            # check if subject is assembled but not listed as ready
            # this can happen if only one subject was assembled or last
            if subject_index not in self.predictions:
                raise ValueError('Subject with index {} not in assembler'.format(subject_index))
        assembled = self.predictions.pop(subject_index)
        if '__prediction' in assembled:
            return assembled['__prediction']
        return assembled
