from __future__ import division  # py3 "true division"

from itertools import chain
import logging

try:
    from queue import Queue, Empty
except ImportError:
    from Queue import Queue, Empty  # noqa:F401

from numpy import dot, float32 as REAL, memmap as np_memmap, \
    double, array, zeros, vstack, sqrt, newaxis, integer, \
    ndarray, sum as np_sum, prod, argmax
import numpy as np
from six import string_types, integer_types
from six.moves import zip, range
from scipy import stats


from . import utils, matutils  # utility fnc for pickling, common scipy operations etc
from .dictionary import Dictionary
from .termsim import TermSimilarityIndex, SparseTermSimilarityMatrix
from .utils import deprecated
from .utils_any2vec import _save_word2vec_format, _load_word2vec_format #, ft_ngram_hashes


logger = logging.getLogger(__name__)

# items
class Vocab(object):
    """A single vocabulary item, used internally for collecting per-word frequency/sampling info,
    and for constructing binary trees (incl. both word leaves and inner nodes).
    # vocab means word_info
    """
    def __init__(self, **kwargs):
        self.count = 0
        self.__dict__.update(kwargs)

    def __lt__(self, other):  # used for sorting in a priority queue
        return self.count < other.count

    def __str__(self):
        vals = ['%s:%r' % (key, self.__dict__[key]) for key in sorted(self.__dict__) if not key.startswith('_')]
        return "%s(%s)" % (self.__class__.__name__, ', '.join(vals))

class BaseKeyedVectors(utils.SaveLoad):
    """Abstract base class / interface for various types of word vectors."""
    def __init__(self, vector_size):
        self.vectors = zeros((0, vector_size))
        self.vocab = {}
        self.vector_size = vector_size
        self.index2entity = []

    def save(self, fname_or_handle, **kwargs):   
        super(BaseKeyedVectors, self).save(fname_or_handle, **kwargs)

    @classmethod
    def load(cls, fname_or_handle, **kwargs):
        return super(BaseKeyedVectors, cls).load(fname_or_handle, **kwargs)

    def similarity(self, entity1, entity2):
        """Compute cosine similarity between two entities, specified by their string id."""
        raise NotImplementedError()

    def most_similar(self, **kwargs):
        """Find the top-N most similar entities.
        Possibly have `positive` and `negative` list of entities in `**kwargs`.

        """
        return NotImplementedError()

    def distance(self, entity1, entity2):
        """Compute distance between vectors of two input entities, specified by their string id."""
        raise NotImplementedError()

    def distances(self, entity1, other_entities=()):
        """Compute distances from a given entity (its string id) to all entities in `other_entity`.
        If `other_entities` is empty, return the distance between `entity1` and all entities in vocab.

        """
        raise NotImplementedError()

    def get_vector(self, entity):
        """Get the entity's representations in vector space, as a 1D numpy array.

        Parameters
        ----------
        entity : str
            Identifier of the entity to return the vector for.

        Returns
        -------
        numpy.ndarray
            Vector for the specified entity.

        Raises
        ------
        KeyError
            If the given entity identifier doesn't exist.

        """
        if entity in self.vocab:
            # self.vocab[entity].index: get the ordered vocab_index
            result = self.vectors[self.vocab[entity].index]
            result.setflags(write=False)
            return result
        else:
            raise KeyError("'%s' not in vocabulary" % entity)

    def add(self, entities, weights, replace=False):
        """Append entities and theirs vectors in a manual way.
        If some entity is already in the vocabulary, the old vector is kept unless `replace` flag is True.

        Parameters
        ----------
        entities : list of str
            Entities specified by string ids.
        weights: list of numpy.ndarray or numpy.ndarray
            List of 1D np.array vectors or a 2D np.array of vectors.
        replace: bool, optional
            Flag indicating whether to replace vectors for entities which already exist in the vocabulary,
            if True - replace vectors, otherwise - keep old vectors.

        """
        if isinstance(entities, string_types):
            entities = [entities]
            weights = np.array(weights).reshape(1, -1)
        elif isinstance(weights, list):
            weights = np.array(weights)

        in_vocab_mask = np.zeros(len(entities), dtype=np.bool)
        for idx, entity in enumerate(entities):
            if entity in self.vocab:
                in_vocab_mask[idx] = True

        # add new entities to the vocab
        for idx in np.nonzero(~in_vocab_mask)[0]:
            entity = entities[idx]
            self.vocab[entity] = Vocab(index=len(self.vocab), count=1)
            self.index2entity.append(entity)

        # add vectors for new entities
        self.vectors = vstack((self.vectors, weights[~in_vocab_mask]))

        # change vectors for in_vocab entities if `replace` flag is specified
        if replace:
            in_vocab_idxs = [self.vocab[entities[idx]].index for idx in np.nonzero(in_vocab_mask)[0]]
            self.vectors[in_vocab_idxs] = weights[in_vocab_mask]

    def __setitem__(self, entities, weights):
        """Add entities and theirs vectors in a manual way.
        If some entity is already in the vocabulary, old vector is replaced with the new one.
        This method is alias for :meth:`~gensim.models.keyedvectors.BaseKeyedVectors.add` with `replace=True`.

        Parameters
        ----------
        entities : {str, list of str}
            Entities specified by their string ids.
        weights: list of numpy.ndarray or numpy.ndarray
            List of 1D np.array vectors or 2D np.array of vectors.

        """
        if not isinstance(entities, list):
            entities = [entities]
            weights = weights.reshape(1, -1)

        self.add(entities, weights, replace=True)

    def __getitem__(self, entities):
        """Get vector representation of `entities`.

        Parameters
        ----------
        entities : {str, list of str}
            Input entity/entities.

        Returns
        -------
        numpy.ndarray
            Vector representation for `entities` (1D if `entities` is string, otherwise - 2D).

        """
        if isinstance(entities, string_types):
            # allow calls like trained_model['office'], as a shorthand for trained_model[['office']]
            return self.get_vector(entities)

        return vstack([self.get_vector(entity) for entity in entities])

    def __contains__(self, entity):
        return entity in self.vocab

    def most_similar_to_given(self, entity1, entities_list):
        """Get the `entity` from `entities_list` most similar to `entity1`."""
        return entities_list[argmax([self.similarity(entity1, entity) for entity in entities_list])]

    def closer_than(self, entity1, entity2):
        """Get all entities that are closer to `entity1` than `entity2` is to `entity1`."""
        all_distances = self.distances(entity1)
        e1_index = self.vocab[entity1].index
        e2_index = self.vocab[entity2].index
        closer_node_indices = np.where(all_distances < all_distances[e2_index])[0]
        return [self.index2entity[index] for index in closer_node_indices if index != e1_index]

    def rank(self, entity1, entity2):
        """Rank of the distance of `entity2` from `entity1`, in relation to distances of all entities from `entity1`."""
        return len(self.closer_than(entity1, entity2)) + 1


sim_file1 = 'fieldembed/sources/240.txt'
sim_file2 = 'fieldembed/sources/297.txt'
ana_f     = 'fieldembed/sources/analogy.txt'

class WordEmbeddingsKeyedVectors(BaseKeyedVectors):
    """Class containing common methods for operations over word vectors."""
    def __init__(self, vector_size, GU = None):
        super(WordEmbeddingsKeyedVectors, self).__init__(vector_size=vector_size)
        self.vectors_norm = None
        self.GU  = GU
        # will update index2word and vocab as soon as possible.
        self.vector = None
        ######## The following parts are only suitable for the sub field channels ##########

        self.TU = None
        self.LKP     = None 
        self._derivative_wv = None
        ####################################################################################

    @property
    def derivative_wv(self):
        # if getattr(self, '_derivative_wv', None):
        if getattr(self, '_derivative_wv', None) is not None:
            return self._derivative_wv
        elif getattr(self, 'LKP', None) is not None:
            derivative_wv = WordEmbeddingsKeyedVectors(self.vector_size, GU = self.TU)
            derivative_vectors = zeros((len(self.TU[0]), self.vector_size), dtype=REAL)
            for word_vocidx in range(1, len(self.TU[0])):
                grain_vocidx = self.LKP[word_vocidx]
                if len(grain_vocidx) == 0:
                    field_word = zeros(self.vector_size, dtype=REAL)
                else:
                    field_word = self.vectors[grain_vocidx].mean(axis=0)
                derivative_vectors[word_vocidx] = field_word
            derivative_wv.vectors = derivative_vectors
            self._derivative_wv = derivative_wv
            return self._derivative_wv
        else:
            return self


    def set_GU_and_TU(self):
        if not self.TU and not self.GU:
            # idx2token = 
            token2idx = {tk: idx for idx, tk in enumerate(self.index2word)}
            self.TU = self.index2word, token2idx
            self.GU = self.TU
        else:
            pass

    def lexical_evals(self):
        if getattr(self, 'LKP', None) is not None:
            raise('This is for token level embeddings')
        d = {}
        pearson, spearman, oov_ratio = self.evaluate_word_pairs(sim_file1, restrict_vocab=500000, case_insensitive=False)
        d['sim240_spearman'] = spearman.correlation
        pearson, spearman, oov_ratio = self.evaluate_word_pairs(sim_file2, restrict_vocab=500000, case_insensitive=False)
        d['sim297_spearman'] = spearman.correlation
        
        analogies_score, sections = self.evaluate_word_analogies(ana_f, restrict_vocab=500000, case_insensitive=False)
        for section in sections:
            correct = len(section['correct'])
            total = len(section['correct']) + len(section['incorrect'])
            d['ana_' + section['section'] ] = correct/total
        return d

    ########################
    @property
    def index2entity(self):
        return self.index2word

    @index2entity.setter
    def index2entity(self, value):
        self.index2word = value

    def __contains__(self, word):
        return word in self.vocab

    def save(self, *args, **kwargs):
        """Save KeyedVectors.

        Parameters
        ----------
        fname : str
            Path to the output file.

        See Also
        --------
        :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.load`
            Load saved model.

        """
        # don't bother storing the cached normalized vectors
        kwargs['ignore'] = kwargs.get('ignore', ['vectors_norm', '_derivative_wv'])
        super(WordEmbeddingsKeyedVectors, self).save(*args, **kwargs)

    def word_vec(self, word, use_norm=False):
        """Get `word` representations in vector space, as a 1D numpy array.

        Parameters
        ----------
        word : str
            Input word
        use_norm : bool, optional
            If True - resulting vector will be L2-normalized (unit euclidean length).

        Returns
        -------
        numpy.ndarray
            Vector representation of `word`.

        Raises
        ------
        KeyError
            If word not in vocabulary.

        """
        if word in self.vocab:
            if use_norm:
                result = self.vectors_norm[self.vocab[word].index]
            else:
                result = self.vectors[self.vocab[word].index]

            result.setflags(write=False)
            return result
        elif self.GU:
            if word in self.GU[1]:
                result = self.vectors[self.GU[1][word]]
                return result
        else:
            raise KeyError("word '%s' not in vocabulary" % word)

    def get_vector(self, word):
        return self.word_vec(word)

    def words_closer_than(self, w1, w2):
        """Get all words that are closer to `w1` than `w2` is to `w1`.

        Parameters
        ----------
        w1 : str
            Input word.
        w2 : str
            Input word.

        Returns
        -------
        list (str)
            List of words that are closer to `w1` than `w2` is to `w1`.

        """
        return super(WordEmbeddingsKeyedVectors, self).closer_than(w1, w2)

    def most_similar(self, positive=None, negative=None, topn=10, restrict_vocab=None, indexer=None):
        """Find the top-N most similar words.
        Positive words contribute positively towards the similarity, negative words negatively.

        This method computes cosine similarity between a simple mean of the projection
        weight vectors of the given words and the vectors for each word in the model.
        The method corresponds to the `word-analogy` and `distance` scripts in the original
        word2vec implementation.

        Parameters
        ----------
        positive : list of str, optional
            List of words that contribute positively.
        negative : list of str, optional
            List of words that contribute negatively.
        topn : int or None, optional
            Number of top-N similar words to return, when `topn` is int. When `topn` is None,
            then similarities for all words are returned.
        restrict_vocab : int, optional
            Optional integer which limits the range of vectors which
            are searched for most-similar values. For example, restrict_vocab=10000 would
            only check the first 10000 word vectors in the vocabulary order. (This may be
            meaningful if you've sorted the vocabulary by descending frequency.)

        Returns
        -------
        list of (str, float) or numpy.array
            When `topn` is int, a sequence of (word, similarity) is returned.
            When `topn` is None, then similarities for all words are returned as a
            one-dimensional numpy array with the size of the vocabulary.

        """
        if isinstance(topn, int) and topn < 1:
            return []

        if positive is None:
            positive = []
        if negative is None:
            negative = []

        self.init_sims()

        if isinstance(positive, string_types) and not negative:
            # allow calls like most_similar('dog'), as a shorthand for most_similar(['dog'])
            positive = [positive]

        # add weights for each word, if not already present; default to 1.0 for positive and -1.0 for negative words
        positive = [
            (word, 1.0) if isinstance(word, string_types + (ndarray,)) else word
            for word in positive
        ]
        negative = [
            (word, -1.0) if isinstance(word, string_types + (ndarray,)) else word
            for word in negative
        ]

        # compute the weighted average of all words
        all_words, mean = set(), []
        for word, weight in positive + negative:
            if isinstance(word, ndarray):
                mean.append(weight * word)
            else:
                mean.append(weight * self.word_vec(word, use_norm=True))
                if word in self.vocab:
                    all_words.add(self.vocab[word].index)
        if not mean:
            raise ValueError("cannot compute similarity with no input")
        mean = matutils.unitvec(array(mean).mean(axis=0)).astype(REAL)

        if indexer is not None and isinstance(topn, int):
            return indexer.most_similar(mean, topn)

        limited = self.vectors_norm if restrict_vocab is None else self.vectors_norm[:restrict_vocab]
        dists = dot(limited, mean)
        if not topn:
            return dists
        best = matutils.argsort(dists, topn=topn + len(all_words), reverse=True)
        # ignore (don't return) words from the input
        result = [(self.index2word[sim], float(dists[sim])) for sim in best if sim not in all_words]
        return result[:topn]

    def similar_by_word(self, word, topn=10, restrict_vocab=None):
        """Find the top-N most similar words.

        Parameters
        ----------
        word : str
            Word
        topn : int or None, optional
            Number of top-N similar words to return. If topn is None, similar_by_word returns
            the vector of similarity scores.
        restrict_vocab : int, optional
            Optional integer which limits the range of vectors which
            are searched for most-similar values. For example, restrict_vocab=10000 would
            only check the first 10000 word vectors in the vocabulary order. (This may be
            meaningful if you've sorted the vocabulary by descending frequency.)

        Returns
        -------
        list of (str, float) or numpy.array
            When `topn` is int, a sequence of (word, similarity) is returned.
            When `topn` is None, then similarities for all words are returned as a
            one-dimensional numpy array with the size of the vocabulary.

        """
        return self.most_similar(positive=[word], topn=topn, restrict_vocab=restrict_vocab)

    def similar_by_vector(self, vector, topn=10, restrict_vocab=None):
        """Find the top-N most similar words by vector.

        Parameters
        ----------
        vector : numpy.array
            Vector from which similarities are to be computed.
        topn : int or None, optional
            Number of top-N similar words to return, when `topn` is int. When `topn` is None,
            then similarities for all words are returned.
        restrict_vocab : int, optional
            Optional integer which limits the range of vectors which
            are searched for most-similar values. For example, restrict_vocab=10000 would
            only check the first 10000 word vectors in the vocabulary order. (This may be
            meaningful if you've sorted the vocabulary by descending frequency.)

        Returns
        -------
        list of (str, float) or numpy.array
            When `topn` is int, a sequence of (word, similarity) is returned.
            When `topn` is None, then similarities for all words are returned as a
            one-dimensional numpy array with the size of the vocabulary.

        """
        return self.most_similar(positive=[vector], topn=topn, restrict_vocab=restrict_vocab)

    def wmdistance(self, document1, document2):
        """Compute the Word Mover's Distance between two documents.

        When using this code, please consider citing the following papers:

        * `Ofir Pele and Michael Werman "A linear time histogram metric for improved SIFT matching"
          <http://www.cs.huji.ac.il/~werman/Papers/ECCV2008.pdf>`_
        * `Ofir Pele and Michael Werman "Fast and robust earth mover's distances"
          <https://ieeexplore.ieee.org/document/5459199/>`_
        * `Matt Kusner et al. "From Word Embeddings To Document Distances"
          <http://proceedings.mlr.press/v37/kusnerb15.pdf>`_.

        Parameters
        ----------
        document1 : list of str
            Input document.
        document2 : list of str
            Input document.

        Returns
        -------
        float
            Word Mover's distance between `document1` and `document2`.

        Warnings
        --------
        This method only works if `pyemd <https://pypi.org/project/pyemd/>`_ is installed.

        If one of the documents have no words that exist in the vocab, `float('inf')` (i.e. infinity)
        will be returned.

        Raises
        ------
        ImportError
            If `pyemd <https://pypi.org/project/pyemd/>`_  isn't installed.

        """

        # If pyemd C extension is available, import it.
        # If pyemd is attempted to be used, but isn't installed, ImportError will be raised in wmdistance
        from pyemd import emd

        # Remove out-of-vocabulary words.
        len_pre_oov1 = len(document1)
        len_pre_oov2 = len(document2)
        document1 = [token for token in document1 if token in self]
        document2 = [token for token in document2 if token in self]
        diff1 = len_pre_oov1 - len(document1)
        diff2 = len_pre_oov2 - len(document2)
        if diff1 > 0 or diff2 > 0:
            logger.info('Removed %d and %d OOV words from document 1 and 2 (respectively).', diff1, diff2)

        if not document1 or not document2:
            logger.info(
                "At least one of the documents had no words that were in the vocabulary. "
                "Aborting (returning inf)."
            )
            return float('inf')

        dictionary = Dictionary(documents=[document1, document2])
        vocab_len = len(dictionary)

        if vocab_len == 1:
            # Both documents are composed by a single unique token
            return 0.0

        # Sets for faster look-up.
        docset1 = set(document1)
        docset2 = set(document2)

        # Compute distance matrix.
        distance_matrix = zeros((vocab_len, vocab_len), dtype=double)
        for i, t1 in dictionary.items():
            if t1 not in docset1:
                continue

            for j, t2 in dictionary.items():
                if t2 not in docset2 or distance_matrix[i, j] != 0.0:
                    continue

                # Compute Euclidean distance between word vectors.
                distance_matrix[i, j] = distance_matrix[j, i] = sqrt(np_sum((self[t1] - self[t2])**2))

        if np_sum(distance_matrix) == 0.0:
            # `emd` gets stuck if the distance matrix contains only zeros.
            logger.info('The distance matrix is all zeros. Aborting (returning inf).')
            return float('inf')

        def nbow(document):
            d = zeros(vocab_len, dtype=double)
            nbow = dictionary.doc2bow(document)  # Word frequencies.
            doc_len = len(document)
            for idx, freq in nbow:
                d[idx] = freq / float(doc_len)  # Normalized word frequencies.
            return d

        # Compute nBOW representation of documents.
        d1 = nbow(document1)
        d2 = nbow(document2)

        # Compute WMD.
        return emd(d1, d2, distance_matrix)

    def most_similar_cosmul(self, positive=None, negative=None, topn=10):
        """Find the top-N most similar words, using the multiplicative combination objective,
        proposed by `Omer Levy and Yoav Goldberg "Linguistic Regularities in Sparse and Explicit Word Representations"
        <http://www.aclweb.org/anthology/W14-1618>`_. Positive words still contribute positively towards the similarity,
        negative words negatively, but with less susceptibility to one large distance dominating the calculation.
        In the common analogy-solving case, of two positive and one negative examples,
        this method is equivalent to the "3CosMul" objective (equation (4)) of Levy and Goldberg.

        Additional positive or negative examples contribute to the numerator or denominator,
        respectively - a potentially sensible but untested extension of the method.
        With a single positive example, rankings will be the same as in the default
        :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.most_similar`.

        Parameters
        ----------
        positive : list of str, optional
            List of words that contribute positively.
        negative : list of str, optional
            List of words that contribute negatively.
        topn : int or None, optional
            Number of top-N similar words to return, when `topn` is int. When `topn` is None,
            then similarities for all words are returned.

        Returns
        -------
        list of (str, float) or numpy.array
            When `topn` is int, a sequence of (word, similarity) is returned.
            When `topn` is None, then similarities for all words are returned as a
            one-dimensional numpy array with the size of the vocabulary.

        """
        if isinstance(topn, int) and topn < 1:
            return []

        if positive is None:
            positive = []
        if negative is None:
            negative = []

        self.init_sims()

        if isinstance(positive, string_types) and not negative:
            # allow calls like most_similar_cosmul('dog'), as a shorthand for most_similar_cosmul(['dog'])
            positive = [positive]

        all_words = {
            self.vocab[word].index for word in positive + negative
            if not isinstance(word, ndarray) and word in self.vocab
            }

        positive = [
            self.word_vec(word, use_norm=True) if isinstance(word, string_types) else word
            for word in positive
        ]
        negative = [
            self.word_vec(word, use_norm=True) if isinstance(word, string_types) else word
            for word in negative
        ]

        if not positive:
            raise ValueError("cannot compute similarity with no input")

        # equation (4) of Levy & Goldberg "Linguistic Regularities...",
        # with distances shifted to [0,1] per footnote (7)
        pos_dists = [((1 + dot(self.vectors_norm, term)) / 2) for term in positive]
        neg_dists = [((1 + dot(self.vectors_norm, term)) / 2) for term in negative]
        dists = prod(pos_dists, axis=0) / (prod(neg_dists, axis=0) + 0.000001)

        if not topn:
            return dists
        best = matutils.argsort(dists, topn=topn + len(all_words), reverse=True)
        # ignore (don't return) words from the input
        result = [(self.index2word[sim], float(dists[sim])) for sim in best if sim not in all_words]
        return result[:topn]

    def doesnt_match(self, words):
        """Which word from the given list doesn't go with the others?

        Parameters
        ----------
        words : list of str
            List of words.

        Returns
        -------
        str
            The word further away from the mean of all words.

        """
        self.init_sims()

        used_words = [word for word in words if word in self]
        if len(used_words) != len(words):
            ignored_words = set(words) - set(used_words)
            logger.warning("vectors for words %s are not present in the model, ignoring these words", ignored_words)
        if not used_words:
            raise ValueError("cannot select a word from an empty list")
        vectors = vstack(self.word_vec(word, use_norm=True) for word in used_words).astype(REAL)
        mean = matutils.unitvec(vectors.mean(axis=0)).astype(REAL)
        dists = dot(vectors, mean)
        return sorted(zip(dists, used_words))[0][1]

    @staticmethod
    def cosine_similarities(vector_1, vectors_all):
        """Compute cosine similarities between one vector and a set of other vectors.

        Parameters
        ----------
        vector_1 : numpy.ndarray
            Vector from which similarities are to be computed, expected shape (dim,).
        vectors_all : numpy.ndarray
            For each row in vectors_all, distance from vector_1 is computed, expected shape (num_vectors, dim).

        Returns
        -------
        numpy.ndarray
            Contains cosine distance between `vector_1` and each row in `vectors_all`, shape (num_vectors,).

        """
        norm = np.linalg.norm(vector_1)
        all_norms = np.linalg.norm(vectors_all, axis=1)
        dot_products = dot(vectors_all, vector_1)
        similarities = dot_products / (norm * all_norms)
        return similarities

    def distances(self, word_or_vector, other_words=()):
        """Compute cosine distances from given word or vector to all words in `other_words`.
        If `other_words` is empty, return distance between `word_or_vectors` and all words in vocab.

        Parameters
        ----------
        word_or_vector : {str, numpy.ndarray}
            Word or vector from which distances are to be computed.
        other_words : iterable of str
            For each word in `other_words` distance from `word_or_vector` is computed.
            If None or empty, distance of `word_or_vector` from all words in vocab is computed (including itself).

        Returns
        -------
        numpy.array
            Array containing distances to all words in `other_words` from input `word_or_vector`.

        Raises
        -----
        KeyError
            If either `word_or_vector` or any word in `other_words` is absent from vocab.

        """
        if isinstance(word_or_vector, string_types):
            input_vector = self.word_vec(word_or_vector)
        else:
            input_vector = word_or_vector
        if not other_words:
            other_vectors = self.vectors
        else:
            other_indices = [self.vocab[word].index for word in other_words]
            other_vectors = self.vectors[other_indices]
        return 1 - self.cosine_similarities(input_vector, other_vectors)

    def distance(self, w1, w2):
        """Compute cosine distance between two words.
        Calculate 1 - :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.similarity`.

        Parameters
        ----------
        w1 : str
            Input word.
        w2 : str
            Input word.

        Returns
        -------
        float
            Distance between `w1` and `w2`.

        """
        return 1 - self.similarity(w1, w2)

    def similarity(self, w1, w2):
        """Compute cosine similarity between two words.

        Parameters
        ----------
        w1 : str
            Input word.
        w2 : str
            Input word.

        Returns
        -------
        float
            Cosine similarity between `w1` and `w2`.

        """
        return dot(matutils.unitvec(self[w1]), matutils.unitvec(self[w2]))

    def n_similarity(self, ws1, ws2):
        """Compute cosine similarity between two sets of words.

        Parameters
        ----------
        ws1 : list of str
            Sequence of words.
        ws2: list of str
            Sequence of words.

        Returns
        -------
        numpy.ndarray
            Similarities between `ws1` and `ws2`.

        """
        if not(len(ws1) and len(ws2)):
            raise ZeroDivisionError('At least one of the passed list is empty.')
        v1 = [self[word] for word in ws1]
        v2 = [self[word] for word in ws2]
        return dot(matutils.unitvec(array(v1).mean(axis=0)), matutils.unitvec(array(v2).mean(axis=0)))

    @staticmethod
    def _log_evaluate_word_analogies(section):
        """Calculate score by section, helper for
        :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.evaluate_word_analogies`.

        Parameters
        ----------
        section : dict of (str, (str, str, str, str))
            Section given from evaluation.

        Returns
        -------
        float
            Accuracy score.

        """
        correct, incorrect = len(section['correct']), len(section['incorrect'])
        if correct + incorrect > 0:
            score = correct / (correct + incorrect)
            logger.info("%s: %.1f%% (%i/%i)", section['section'], 100.0 * score, correct, correct + incorrect)
            return score

    def evaluate_word_analogies(self, analogies, restrict_vocab=500000, case_insensitive=True, dummy4unknown=False):
        """Compute performance of the model on an analogy test set.

        This is modern variant of :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.accuracy`, see
        `discussion on GitHub #1935 <https://github.com/RaRe-Technologies/gensim/pull/1935>`_.

        The accuracy is reported (printed to log and returned as a score) for each section separately,
        plus there's one aggregate summary at the end.

        This method corresponds to the `compute-accuracy` script of the original C word2vec.
        See also `Analogy (State of the art) <https://aclweb.org/aclwiki/Analogy_(State_of_the_art)>`_.

        Parameters
        ----------
        analogies : str
            Path to file, where lines are 4-tuples of words, split into sections by ": SECTION NAME" lines.
            See `gensim/test/test_data/questions-words.txt` as example.
        restrict_vocab : int, optional
            Ignore all 4-tuples containing a word not in the first `restrict_vocab` words.
            This may be meaningful if you've sorted the model vocabulary by descending frequency (which is standard
            in modern word embedding models).
        case_insensitive : bool, optional
            If True - convert all words to their uppercase form before evaluating the performance.
            Useful to handle case-mismatch between training tokens and words in the test set.
            In case of multiple case variants of a single word, the vector for the first occurrence
            (also the most frequent if vocabulary is sorted) is taken.
        dummy4unknown : bool, optional
            If True - produce zero accuracies for 4-tuples with out-of-vocabulary words.
            Otherwise, these tuples are skipped entirely and not used in the evaluation.

        Returns
        -------
        score : float
            The overall evaluation score on the entire evaluation set
        sections : list of dict of {str : str or list of tuple of (str, str, str, str)}
            Results broken down by each section of the evaluation set. Each dict contains the name of the section
            under the key 'section', and lists of correctly and incorrectly predicted 4-tuples of words under the
            keys 'correct' and 'incorrect'.

        """
        ok_vocab = [(w, self.vocab[w]) for w in self.index2word[:restrict_vocab]]
        ok_vocab = {w.upper(): v for w, v in reversed(ok_vocab)} if case_insensitive else dict(ok_vocab)
        oov = 0
        logger.info("Evaluating word analogies for top %i words in the model on %s", restrict_vocab, analogies)
        sections, section = [], None
        quadruplets_no = 0
        for line_no, line in enumerate(utils.smart_open(analogies)):
            line = utils.to_unicode(line)
            if line.startswith(': '):
                # a new section starts => store the old section
                if section:
                    sections.append(section)
                    self._log_evaluate_word_analogies(section)
                section = {'section': line.lstrip(': ').strip(), 'correct': [], 'incorrect': []}
            else:
                if not section:
                    raise ValueError("Missing section header before line #%i in %s" % (line_no, analogies))
                try:
                    if case_insensitive:
                        a, b, c, expected = [word.upper() for word in line.split()]
                    else:
                        a, b, c, expected = [word for word in line.split()]
                except ValueError:
                    logger.info("Skipping invalid line #%i in %s", line_no, analogies)
                    continue
                quadruplets_no += 1
                if a not in ok_vocab or b not in ok_vocab or c not in ok_vocab or expected not in ok_vocab:
                    oov += 1
                    if dummy4unknown:
                        logger.debug('Zero accuracy for line #%d with OOV words: %s', line_no, line.strip())
                        section['incorrect'].append((a, b, c, expected))
                    else:
                        logger.debug("Skipping line #%i with OOV words: %s", line_no, line.strip())
                    continue
                original_vocab = self.vocab
                self.vocab = ok_vocab
                ignore = {a, b, c}  # input words to be ignored
                predicted = None
                # find the most likely prediction using 3CosAdd (vector offset) method
                # TODO: implement 3CosMul and set-based methods for solving analogies
                sims = self.most_similar(positive=[b, c], negative=[a], topn=5, restrict_vocab=restrict_vocab)
                self.vocab = original_vocab
                for element in sims:
                    predicted = element[0].upper() if case_insensitive else element[0]
                    if predicted in ok_vocab and predicted not in ignore:
                        if predicted != expected:
                            logger.debug("%s: expected %s, predicted %s", line.strip(), expected, predicted)
                        break
                if predicted == expected:
                    section['correct'].append((a, b, c, expected))
                else:
                    section['incorrect'].append((a, b, c, expected))
        if section:
            # store the last section, too
            sections.append(section)
            self._log_evaluate_word_analogies(section)

        total = {
            'section': 'Total accuracy',
            'correct': list(chain.from_iterable(s['correct'] for s in sections)),
            'incorrect': list(chain.from_iterable(s['incorrect'] for s in sections)),
        }

        oov_ratio = float(oov) / quadruplets_no * 100
        logger.info('Quadruplets with out-of-vocabulary words: %.1f%%', oov_ratio)
        if not dummy4unknown:
            logger.info(
                'NB: analogies containing OOV words were skipped from evaluation! '
                'To change this behavior, use "dummy4unknown=True"'
            )
        analogies_score = self._log_evaluate_word_analogies(total)
        sections.append(total)
        # Return the overall score and the full lists of correct and incorrect analogies
        return analogies_score, sections

    @staticmethod
    def log_accuracy(section):
        correct, incorrect = len(section['correct']), len(section['incorrect'])
        if correct + incorrect > 0:
            logger.info(
                "%s: %.1f%% (%i/%i)",
                section['section'], 100.0 * correct / (correct + incorrect), correct, correct + incorrect
            )

    @staticmethod
    def _log_evaluate_word_pairs(pearson, spearman, oov, pairs):
        logger.info('Pearson correlation coefficient against %s: %.4f', pairs, pearson[0])
        logger.info('Spearman rank-order correlation coefficient against %s: %.4f', pairs, spearman[0])
        logger.info('Pairs with unknown words ratio: %.1f%%', oov)

    def evaluate_word_pairs(self, pairs, delimiter='\t', restrict_vocab=500000, case_insensitive=True, dummy4unknown=False):
        """Compute correlation of the model with human similarity judgments.

        Notes
        -----
        More datasets can be found at
        * http://technion.ac.il/~ira.leviant/MultilingualVSMdata.html
        * https://www.cl.cam.ac.uk/~fh295/simlex.html.

        Parameters
        ----------
        pairs : str
            Path to file, where lines are 3-tuples, each consisting of a word pair and a similarity value.
            See `test/test_data/wordsim353.tsv` as example.
        delimiter : str, optional
            Separator in `pairs` file.
        restrict_vocab : int, optional
            Ignore all 4-tuples containing a word not in the first `restrict_vocab` words.
            This may be meaningful if you've sorted the model vocabulary by descending frequency (which is standard
            in modern word embedding models).
        case_insensitive : bool, optional
            If True - convert all words to their uppercase form before evaluating the performance.
            Useful to handle case-mismatch between training tokens and words in the test set.
            In case of multiple case variants of a single word, the vector for the first occurrence
            (also the most frequent if vocabulary is sorted) is taken.
        dummy4unknown : bool, optional
            If True - produce zero accuracies for 4-tuples with out-of-vocabulary words.
            Otherwise, these tuples are skipped entirely and not used in the evaluation.

        Returns
        -------
        pearson : tuple of (float, float)
            Pearson correlation coefficient with 2-tailed p-value.
        spearman : tuple of (float, float)
            Spearman rank-order correlation coefficient between the similarities from the dataset and the
            similarities produced by the model itself, with 2-tailed p-value.
        oov_ratio : float
            The ratio of pairs with unknown words.

        """
        ok_vocab = [(w, self.vocab[w]) for w in self.index2word[:restrict_vocab]]
        ok_vocab = {w.upper(): v for w, v in reversed(ok_vocab)} if case_insensitive else dict(ok_vocab)

        similarity_gold = []
        similarity_model = []
        oov = 0

        original_vocab = self.vocab
        self.vocab = ok_vocab

        for line_no, line in enumerate(utils.smart_open(pairs)):
            line = utils.to_unicode(line)
            if line.startswith('#'):
                # May be a comment
                continue
            else:
                try:
                    if case_insensitive:
                        a, b, sim = [word.upper() for word in line.split(delimiter)]
                    else:
                        a, b, sim = [word for word in line.split(delimiter)]
                    sim = float(sim)
                except (ValueError, TypeError):
                    logger.info('Skipping invalid line #%d in %s', line_no, pairs)
                    continue
                if a not in ok_vocab or b not in ok_vocab:
                    oov += 1
                    if dummy4unknown:
                        logger.debug('Zero similarity for line #%d with OOV words: %s', line_no, line.strip())
                        similarity_model.append(0.0)
                        similarity_gold.append(sim)
                        continue
                    else:
                        logger.debug('Skipping line #%d with OOV words: %s', line_no, line.strip())
                        continue
                similarity_gold.append(sim)  # Similarity from the dataset
                similarity_model.append(self.similarity(a, b))  # Similarity from the model
        self.vocab = original_vocab
        spearman = stats.spearmanr(similarity_gold, similarity_model)
        pearson = stats.pearsonr(similarity_gold, similarity_model)
        if dummy4unknown:
            oov_ratio = float(oov) / len(similarity_gold) * 100
        else:
            oov_ratio = float(oov) / (len(similarity_gold) + oov) * 100

        logger.debug('Pearson correlation coefficient against %s: %f with p-value %f', pairs, pearson[0], pearson[1])
        logger.debug(
            'Spearman rank-order correlation coefficient against %s: %f with p-value %f',
            pairs, spearman[0], spearman[1]
        )
        logger.debug('Pairs with unknown words: %d', oov)
        self._log_evaluate_word_pairs(pearson, spearman, oov_ratio, pairs)
        return pearson, spearman, oov_ratio

    def init_sims(self, replace=False):
        """Precompute L2-normalized vectors.

        Parameters
        ----------
        replace : bool, optional
            If True - forget the original vectors and only keep the normalized ones = saves lots of memory!

        Warnings
        --------
        You **cannot continue training** after doing a replace.
        The model becomes effectively read-only: you can call
        :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.most_similar`,
        :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.similarity`, etc., but not train.

        """
        if getattr(self, 'vectors_norm', None) is None or replace:
            logger.info("precomputing L2-norms of word weight vectors")
            self.vectors_norm = _l2_norm(self.vectors, replace=replace)

    def relative_cosine_similarity(self, wa, wb, topn=10):
        """Compute the relative cosine similarity between two words given top-n similar words,
        by `Artuur Leeuwenberga, Mihaela Velab , Jon Dehdaribc, Josef van Genabithbc "A Minimally Supervised Approach
        for Synonym Extraction with Word Embeddings" <https://ufal.mff.cuni.cz/pbml/105/art-leeuwenberg-et-al.pdf>`_.

        To calculate relative cosine similarity between two words, equation (1) of the paper is used.
        For WordNet synonyms, if rcs(topn=10) is greater than 0.10 then wa and wb are more similar than
        any arbitrary word pairs.

        Parameters
        ----------
        wa: str
            Word for which we have to look top-n similar word.
        wb: str
            Word for which we evaluating relative cosine similarity with wa.
        topn: int, optional
            Number of top-n similar words to look with respect to wa.

        Returns
        -------
        numpy.float64
            Relative cosine similarity between wa and wb.

        """
        sims = self.similar_by_word(wa, topn)
        assert sims, "Failed code invariant: list of similar words must never be empty."
        rcs = float(self.similarity(wa, wb)) / (sum(sim for _, sim in sims))

        return rcs



class Word2VecKeyedVectors(WordEmbeddingsKeyedVectors):
    """Mapping between words and vectors for the :class:`~gensim.models.Word2Vec` model.
    Used to perform operations on the vectors such as vector lookup, distance, similarity etc.

    """
    def save_word2vec_format(self, fname, fvocab=None, binary=False, total_vec=None):
        """Store the input-hidden weight matrix in the same format used by the original
        C word2vec-tool, for compatibility.

        Parameters
        ----------
        fname : str
            The file path used to save the vectors in
        fvocab : str, optional
            Optional file path used to save the vocabulary
        binary : bool, optional
            If True, the data will be saved in binary word2vec format, else it will be saved in plain text.
        total_vec : int, optional
            Optional parameter to explicitly specify total no. of vectors
            (in case word vectors are appended with document vectors afterwards).

        """
        # from gensim.models.word2vec import save_word2vec_format
        _save_word2vec_format(
            fname, self.vocab, self.vectors, fvocab=fvocab, binary=binary, total_vec=total_vec)

    @classmethod
    def load_word2vec_format(cls, fname, fvocab=None, binary=False, encoding='utf8', unicode_errors='strict',limit=None, datatype=REAL, sep = ' '):
        """Load the input-hidden weight matrix from the original C word2vec-tool format.

        Warnings
        --------
        The information stored in the file is incomplete (the binary tree is missing),
        so while you can query for word similarity etc., you cannot continue training
        with a model loaded this way.

        Parameters
        ----------
        fname : str
            The file path to the saved word2vec-format file.
        fvocab : str, optional
            File path to the vocabulary.Word counts are read from `fvocab` filename, if set
            (this is the file generated by `-save-vocab` flag of the original C tool).
        binary : bool, optional
            If True, indicates whether the data is in binary word2vec format.
        encoding : str, optional
            If you trained the C model using non-utf8 encoding for words, specify that encoding in `encoding`.
        unicode_errors : str, optional
            default 'strict', is a string suitable to be passed as the `errors`
            argument to the unicode() (Python 2.x) or str() (Python 3.x) function. If your source
            file may include word tokens truncated in the middle of a multibyte unicode character
            (as is common from the original word2vec.c tool), 'ignore' or 'replace' may help.
        limit : int, optional
            Sets a maximum number of word-vectors to read from the file. The default,
            None, means read all.
        datatype : type, optional
            (Experimental) Can coerce dimensions to a non-default float type (such as `np.float16`) to save memory.
            Such types may result in much slower bulk operations or incompatibility with optimized routines.)

        Returns
        -------
        :class:`~gensim.models.keyedvectors.Word2VecKeyedVectors`
            Loaded model.

        """
        # from gensim.models.word2vec import load_word2vec_format
        return _load_word2vec_format(
            cls, fname, fvocab=fvocab, binary=binary, encoding=encoding, unicode_errors=unicode_errors,
            limit=limit, datatype=datatype, sep = sep)

    @classmethod
    def load_old_wv_format(cls, old_wv):
        wv = Word2VecKeyedVectors(wv_old.vector_size)
        wv.index2word = wv_old.index2word
        wv.vocab      = wv_old.vocab
        wv.vectors    = wv_old.vectors
        return wv

    def get_keras_embedding(self, train_embeddings=False):
        """Get a Keras 'Embedding' layer with weights set as the Word2Vec model's learned word embeddings.

        Parameters
        ----------
        train_embeddings : bool
            If False, the weights are frozen and stopped from being updated.
            If True, the weights can/will be further trained/updated.

        Returns
        -------
        `keras.layers.Embedding`
            Embedding layer.

        Raises
        ------
        ImportError
            If `Keras <https://pypi.org/project/Keras/>`_ not installed.

        Warnings
        --------
        Current method work only if `Keras <https://pypi.org/project/Keras/>`_ installed.

        """
        try:
            from keras.layers import Embedding
        except ImportError:
            raise ImportError("Please install Keras to use this function")
        weights = self.vectors

        # set `trainable` as `False` to use the pretrained word embedding
        # No extra mem usage here as `Embedding` layer doesn't create any new matrix for weights
        layer = Embedding(
            input_dim=weights.shape[0], output_dim=weights.shape[1],
            weights=[weights], trainable=train_embeddings
        )
        return layer

    @classmethod
    def load(cls, fname_or_handle, **kwargs):
        model = super(WordEmbeddingsKeyedVectors, cls).load(fname_or_handle, **kwargs)
        # if isinstance(model, FastTextKeyedVectors):
        #     if not hasattr(model, 'compatible_hash'):
        #         model.compatible_hash = False

        return model


KeyedVectors = Word2VecKeyedVectors  # alias for backward compatibility

def _process_fasttext_vocab(iterable, min_n, max_n, num_buckets, compatible_hash):
    """
    Performs a common operation for FastText weight initialization and
    updates: scan the vocabulary, calculate ngrams and their hashes, keep
    track of new ngrams, the buckets that each word relates to via its
    ngrams, etc.

    Parameters
    ----------
    iterable : list
        A list of (word, :class:`Vocab`) tuples.
    min_n : int
        The minimum length of ngrams.
    max_n : int
        The maximum length of ngrams.
    num_buckets : int
        The number of buckets used by the model.
    compatible_hash : boolean
        True for compatibility with the Facebook implementation.
        False for compatibility with the old Gensim implementation.

    Returns
    -------
    dict
        Keys are indices of entities in the vocabulary (words).  Values are
        arrays containing indices into vectors_ngrams for each ngram of the
        word.

    """
    word_indices = {}

    if num_buckets == 0:
        return {v.index: np.array([], dtype=np.uint32) for w, v in iterable}

    for word, vocab in iterable:
        wi = []
        for ngram_hash in ft_ngram_hashes(word, min_n, max_n, num_buckets, compatible_hash):
            wi.append(ngram_hash)
        word_indices[vocab.index] = np.array(wi, dtype=np.uint32)

    return word_indices


def _pad_random(m, new_rows, rand):
    """Pad a matrix with additional rows filled with random values."""
    rows, columns = m.shape
    low, high = -1.0 / columns, 1.0 / columns
    suffix = rand.uniform(low, high, (new_rows, columns)).astype(REAL)
    return vstack([m, suffix])


def _l2_norm(m, replace=False):
    """Return an L2-normalized version of a matrix.

    Parameters
    ----------
    m : np.array
        The matrix to normalize.
    replace : boolean, optional
        If True, modifies the existing matrix.

    Returns
    -------
    The normalized matrix.  If replace=True, this will be the same as m.

    """
    dist = sqrt((m ** 2).sum(-1))[..., newaxis]
    if replace:
        m /= dist
        return m
    else:
        return (m / dist).astype(REAL)


def _rollback_optimization(kv):
    """Undo the optimization that pruned buckets.

    This unfortunate optimization saves memory and CPU cycles, but breaks
    compatibility with Facebook's model by introducing divergent behavior
    for OOV words.

    """
    logger.warning(
        "This saved FastText model was trained with an optimization we no longer support. "
        "The current Gensim version automatically reverses this optimization during loading. "
        "Save the loaded model to a new file and reload to suppress this message."
    )
    assert hasattr(kv, 'hash2index')
    assert hasattr(kv, 'num_ngram_vectors')

    kv.vectors_ngrams = _unpack(kv.vectors_ngrams, kv.bucket, kv.hash2index)

    #
    # We have replaced num_ngram_vectors with a property and deprecated it.
    # We can't delete it because the new attribute masks the member.
    #
    del kv.hash2index


def _unpack_copy(m, num_rows, hash2index, seed=1):
    """Same as _unpack, but makes a copy of the matrix.

    Simpler implementation, but uses more RAM.

    """
    rows, columns = m.shape
    if rows == num_rows:
        #
        # Nothing to do.
        #
        return m
    assert num_rows > rows

    rand_obj = np.random
    rand_obj.seed(seed)

    n = np.empty((0, columns), dtype=m.dtype)
    n = _pad_random(n, num_rows, rand_obj)

    for src, dst in hash2index.items():
        n[src] = m[dst]

    return n


def _unpack(m, num_rows, hash2index, seed=1):
    """Restore the array to its natural shape, undoing the optimization.

    A packed matrix contains contiguous vectors for ngrams, as well as a hashmap.
    The hash map maps the ngram hash to its index in the packed matrix.
    To unpack the matrix, we need to do several things:

    1. Restore the matrix to its "natural" shape, where the number of rows
       equals the number of buckets.
    2. Rearrange the existing rows such that the hashmap becomes the identity
       function and is thus redundant.
    3. Fill the new rows with random values.

    Parameters
    ----------

    m : np.ndarray
        The matrix to restore.
    num_rows : int
        The number of rows that this array should have.
    hash2index : dict
        the product of the optimization we are undoing.
    seed : float, optional
        The seed for the PRNG.  Will be used to initialize new rows.

    Returns
    -------
    np.array
        The unpacked matrix.

    Notes
    -----

    The unpacked matrix will reference some rows in the input matrix to save memory.
    Throw away the old matrix after calling this function, or use np.copy.

    """
    orig_rows, orig_columns = m.shape
    if orig_rows == num_rows:
        #
        # Nothing to do.
        #
        return m
    assert num_rows > orig_rows

    rand_obj = np.random
    rand_obj.seed(seed)

    #
    # Rows at the top of the matrix (the first orig_rows) will contain "packed" learned vectors.
    # Rows at the bottom of the matrix will be "free": initialized to random values.
    #
    m = _pad_random(m, num_rows - orig_rows, rand_obj)

    #
    # Swap rows to transform hash2index into the identify function.
    # There are two kinds of swaps.
    # First, rearrange the rows that belong entirely within the original matrix dimensions.
    # Second, swap out rows from the original matrix dimensions, replacing them with
    # randomly initialized values.
    #
    # N.B. We only do the swap in one direction, because doing it in both directions
    # nullifies the effect.
    #
    swap = {h: i for (h, i) in hash2index.items() if h < i < orig_rows}
    swap.update({h: i for (h, i) in hash2index.items() if h >= orig_rows})
    for h, i in swap.items():
        assert h != i
        m[[h, i]] = m[[i, h]]  # swap rows i and h

    return m


def _try_upgrade(wv):
    if hasattr(wv, 'hash2index'):
        _rollback_optimization(wv)

    if not hasattr(wv, 'compatible_hash'):
        logger.warning(
            "This older model was trained with a buggy hash function. "
            "The model will continue to work, but consider training it "
            "from scratch."
        )
        wv.compatible_hash = False


class WordEmbeddingSimilarityIndex(TermSimilarityIndex):
    """
    Computes cosine similarities between word embeddings and retrieves the closest word embeddings
    by cosine similarity for a given word embedding.

    Parameters
    ----------
    keyedvectors : :class:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors`
        The word embeddings.
    threshold : float, optional
        Only embeddings more similar than `threshold` are considered when retrieving word embeddings
        closest to a given word embedding.
    exponent : float, optional
        Take the word embedding similarities larger than `threshold` to the power of `exponent`.
    kwargs : dict or None
        A dict with keyword arguments that will be passed to the `keyedvectors.most_similar` method
        when retrieving the word embeddings closest to a given word embedding.

    See Also
    --------
    :class:`~gensim.similarities.termsim.SparseTermSimilarityMatrix`
        Build a term similarity matrix and compute the Soft Cosine Measure.

    """
    def __init__(self, keyedvectors, threshold=0.0, exponent=2.0, kwargs=None):
        assert isinstance(keyedvectors, WordEmbeddingsKeyedVectors)
        self.keyedvectors = keyedvectors
        self.threshold = threshold
        self.exponent = exponent
        self.kwargs = kwargs or {}
        super(WordEmbeddingSimilarityIndex, self).__init__()

    def most_similar(self, t1, topn=10):
        if t1 not in self.keyedvectors.vocab:
            logger.debug('an out-of-dictionary term "%s"', t1)
        else:
            most_similar = self.keyedvectors.most_similar(positive=[t1], topn=topn, **self.kwargs)
            for t2, similarity in most_similar:
                if similarity > self.threshold:
                    yield (t2, similarity**self.exponent)




from nlptext.sentence import Sentence

def getsent2matrix(sent, wv, train = True):
    features = {}
    Channel_Settings = fieldembed.Field_Settings
    token_strs = [i[0] for i in sent.get_grain_str('token')]
    # print(token_strs)
    features['origin'] = token_strs

    wv = wv.derivative_wv
    TU = derivative_wv.GU # LGU in derivative wv is LTU
    # this code is verbose
    # TODO: how to deal with unk tokens
    token_idxes = [TU[1].get(token_str, 0) for token_str in token_strs] # 0 is not unk, to fix it in the future
    # token_idxes = [i[0] for i in token_idxes]
    # print(token_idxes)
    matrix = derivative_wv.vectors[token_idxes]
    return matrix

def convert_document_to_X_and_Y(nlptext, wv):

    for sentidx in range(nlptext.SENT['length']):
        sent = Sentence(sentidx)
        matrix = getsent2matrix(sent, wv)
        # matrix.append(sent)
        docvector = np.mean(matrix, axis = 1)
    