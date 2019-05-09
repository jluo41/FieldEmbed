from __future__ import division  # py3 "true division"

import logging
import sys
import os
import heapq
from timeit import default_timer
from copy import deepcopy
from collections import defaultdict
import threading
import itertools
import warnings

try:
    from queue import Queue, Empty
except ImportError:
    from Queue import Queue, Empty

from numpy import exp, dot, zeros, random, dtype, float32 as REAL,\
    uint32, seterr, array, uint8, vstack, fromstring, sqrt,\
    empty, sum as np_sum, ones, logaddexp, log, outer

from scipy.special import expit
from six import iteritems, itervalues, string_types
from six.moves import range


from .. import utils, matutils  # utility fnc for pickling, common scipy operations etc
from ..utils import deprecated
from ..utils import keep_vocab_item, call_on_class_only
from .keyedvectors import Vocab, Word2VecKeyedVectors
from .base_any2vec import BaseWordEmbeddingsModel


logger = logging.getLogger(__name__)


try:
    from .word2vec_inner import train_batch_sg, train_batch_cbow
    from .word2vec_inner import FAST_VERSION, MAX_WORDS_IN_BATCH

except ImportError:
    # failed... fall back to plain numpy (20-80x slower training than the above)
    FAST_VERSION = -1
    MAX_WORDS_IN_BATCH = 10000

    def train_batch_sg(model, sentences, alpha, work=None, compute_loss=False):
        result = 0
        for sentence in sentences:
            word_vocabs = [model.wv.vocab[w] for w in sentence if w in model.wv.vocab
                           and model.wv.vocab[w].sample_int > model.random.rand() * 2 ** 32]
            for pos, word in enumerate(word_vocabs):
                reduced_window = model.random.randint(model.window)  # `b` in the original word2vec code

                # now go over all words from the (reduced) window, predicting each one in turn
                start = max(0, pos - model.window + reduced_window)
                for pos2, word2 in enumerate(word_vocabs[start:(pos + model.window + 1 - reduced_window)], start):
                    # don't train on the `word` itself
                    if pos2 != pos:
                        train_sg_pair(
                            model, model.wv.index2word[word.index], word2.index, alpha, compute_loss=compute_loss
                        )

            result += len(word_vocabs)
        return result

    def train_batch_cbow(model, sentences, alpha, work=None, neu1=None, compute_loss=False):
        result = 0
        for sentence in sentences:
            word_vocabs = [
                model.wv.vocab[w] for w in sentence if w in model.wv.vocab
                and model.wv.vocab[w].sample_int > model.random.rand() * 2 ** 32
            ]
            for pos, word in enumerate(word_vocabs):
                reduced_window = model.random.randint(model.window)  # `b` in the original word2vec code
                start = max(0, pos - model.window + reduced_window)
                window_pos = enumerate(word_vocabs[start:(pos + model.window + 1 - reduced_window)], start)
                word2_indices = [word2.index for pos2, word2 in window_pos if (word2 is not None and pos2 != pos)]
                l1 = np_sum(model.wv.syn0[word2_indices], axis=0)  # 1 x vector_size
                if word2_indices and model.cbow_mean:
                    l1 /= len(word2_indices)
                train_cbow_pair(model, word, word2_indices, l1, alpha, compute_loss=compute_loss)
            result += len(word_vocabs)
        return result


def train_sg_pair(model, word, context_index, alpha, learn_vectors=True, learn_hidden=True, context_vectors=None, context_locks=None, compute_loss=False, is_ft=False):

    if context_vectors is None:
        if is_ft:
            context_vectors_vocab = model.wv.syn0_vocab
            context_vectors_ngrams = model.wv.syn0_ngrams
        else:
            context_vectors = model.wv.syn0
    if context_locks is None:
        if is_ft:
            context_locks_vocab = model.syn0_vocab_lockf
            context_locks_ngrams = model.syn0_ngrams_lockf
        else:
            context_locks = model.syn0_lockf

    if word not in model.wv.vocab:
        return
    predict_word = model.wv.vocab[word]  # target word (NN output)

    if is_ft:
        l1_vocab = context_vectors_vocab[context_index[0]]
        l1_ngrams = np_sum(context_vectors_ngrams[context_index[1:]], axis=0)
        if context_index:
            l1 = np_sum([l1_vocab, l1_ngrams], axis=0) / len(context_index)
    else:
        l1 = context_vectors[context_index]  # input word (NN input/projection layer)
        lock_factor = context_locks[context_index]

    neu1e = zeros(l1.shape)

    if model.hs:
        # work on the entire tree at once, to push as much work into numpy's C routines as possible (performance)
        l2a = deepcopy(model.syn1[predict_word.point])  # 2d matrix, codelen x layer1_size
        prod_term = dot(l1, l2a.T)
        fa = expit(prod_term)  # propagate hidden -> output
        ga = (1 - predict_word.code - fa) * alpha  # vector of error gradients multiplied by the learning rate
        if learn_hidden:
            model.syn1[predict_word.point] += outer(ga, l1)  # learn hidden -> output
        neu1e += dot(ga, l2a)  # save error

        # loss component corresponding to hierarchical softmax
        if compute_loss:
            sgn = (-1.0) ** predict_word.code  # `ch` function, 0 -> 1, 1 -> -1
            lprob = -log(expit(-sgn * prod_term))
            model.running_training_loss += sum(lprob)

    if model.negative:
        # use this word (label = 1) + `negative` other random words not from this sentence (label = 0)
        word_indices = [predict_word.index]
        while len(word_indices) < model.negative + 1:
            w = model.cum_table.searchsorted(model.random.randint(model.cum_table[-1]))
            if w != predict_word.index:
                word_indices.append(w)
        l2b = model.syn1neg[word_indices]  # 2d matrix, k+1 x layer1_size
        prod_term = dot(l1, l2b.T)
        fb = expit(prod_term)  # propagate hidden -> output
        gb = (model.neg_labels - fb) * alpha  # vector of error gradients multiplied by the learning rate
        if learn_hidden:
            model.syn1neg[word_indices] += outer(gb, l1)  # learn hidden -> output
        neu1e += dot(gb, l2b)  # save error

        # loss component corresponding to negative sampling
        if compute_loss:
            model.running_training_loss -= sum(log(expit(-1 * prod_term[1:])))  # for the sampled words
            model.running_training_loss -= log(expit(prod_term[0]))  # for the output word

    if learn_vectors:
        if is_ft:
            model.wv.syn0_vocab[context_index[0]] += neu1e * context_locks_vocab[context_index[0]]
            for i in context_index[1:]:
                model.wv.syn0_ngrams[i] += neu1e * context_locks_ngrams[i]
        else:
            l1 += neu1e * lock_factor  # learn input -> hidden (mutates model.wv.syn0[word2.index], if that is l1)
    return neu1e


def train_cbow_pair(model, word, input_word_indices, l1, alpha, learn_vectors=True, learn_hidden=True, compute_loss=False, context_vectors=None, context_locks=None, is_ft=False):

    if context_vectors is None:
        if is_ft:
            context_vectors_vocab = model.wv.syn0_vocab
            context_vectors_ngrams = model.wv.syn0_ngrams
        else:
            context_vectors = model.wv.syn0
    if context_locks is None:
        if is_ft:
            context_locks_vocab = model.syn0_vocab_lockf
            context_locks_ngrams = model.syn0_ngrams_lockf
        else:
            context_locks = model.syn0_lockf

    neu1e = zeros(l1.shape)

    if model.hs:
        l2a = model.syn1[word.point]  # 2d matrix, codelen x layer1_size
        prod_term = dot(l1, l2a.T)
        fa = expit(prod_term)  # propagate hidden -> output
        ga = (1. - word.code - fa) * alpha  # vector of error gradients multiplied by the learning rate
        if learn_hidden:
            model.syn1[word.point] += outer(ga, l1)  # learn hidden -> output
        neu1e += dot(ga, l2a)  # save error

        # loss component corresponding to hierarchical softmax
        if compute_loss:
            sgn = (-1.0) ** word.code  # ch function, 0-> 1, 1 -> -1
            model.running_training_loss += sum(-log(expit(-sgn * prod_term)))

    if model.negative:
        # use this word (label = 1) + `negative` other random words not from this sentence (label = 0)
        word_indices = [word.index]
        while len(word_indices) < model.negative + 1:
            w = model.cum_table.searchsorted(model.random.randint(model.cum_table[-1]))
            if w != word.index:
                word_indices.append(w)
        l2b = model.syn1neg[word_indices]  # 2d matrix, k+1 x layer1_size
        prod_term = dot(l1, l2b.T)
        fb = expit(prod_term)  # propagate hidden -> output
        gb = (model.neg_labels - fb) * alpha  # vector of error gradients multiplied by the learning rate
        if learn_hidden:
            model.syn1neg[word_indices] += outer(gb, l1)  # learn hidden -> output
        neu1e += dot(gb, l2b)  # save error

        # loss component corresponding to negative sampling
        if compute_loss:
            model.running_training_loss -= sum(log(expit(-1 * prod_term[1:])))  # for the sampled words
            model.running_training_loss -= log(expit(prod_term[0]))  # for the output word

    if learn_vectors:
        # learn input -> hidden, here for all words in the window separately
        if is_ft:
            if not model.cbow_mean and input_word_indices:
                neu1e /= (len(input_word_indices[0]) + len(input_word_indices[1]))
            for i in input_word_indices[0]:
                context_vectors_vocab[i] += neu1e * context_locks_vocab[i]
            for i in input_word_indices[1]:
                context_vectors_ngrams[i] += neu1e * context_locks_ngrams[i]
        else:
            if not model.cbow_mean and input_word_indices:
                neu1e /= len(input_word_indices)
            for i in input_word_indices:
                context_vectors[i] += neu1e * context_locks[i]

    return neu1e


class Word2Vec(BaseWordEmbeddingsModel):
    """Train, use and evaluate neural networks described in https://code.google.com/p/word2vec/.

    Once you're finished training a model (=no more updates, only querying)
    store and use only the :class:`~gensim.models.keyedvectors.KeyedVectors` instance in `self.wv` to reduce memory.

    The model can be stored/loaded via its :meth:`~gensim.models.word2vec.Word2Vec.save` and
    :meth:`~gensim.models.word2vec.Word2Vec.load` methods.

    The trained word vectors can also be stored/loaded from a format compatible with the
    original word2vec implementation via `self.wv.save_word2vec_format`
    and :meth:`gensim.models.keyedvectors.KeyedVectors.load_word2vec_format`.

    Some important attributes are the following:

    Attributes
    ----------
    wv : :class:`~gensim.models.keyedvectors.Word2VecKeyedVectors`
        This object essentially contains the mapping between words and embeddings. After training, it can be used
        directly to query those embeddings in various ways. See the module level docstring for examples.

    vocabulary : :class:`~gensim.models.word2vec.Word2VecVocab`
        This object represents the vocabulary (sometimes called Dictionary in gensim) of the model.
        Besides keeping track of all unique words, this object provides extra functionality, such as
        constructing a huffman tree (frequent words are closer to the root), or discarding extremely rare words.

    trainables : :class:`~gensim.models.word2vec.Word2VecTrainables`
        This object represents the inner shallow neural network used to train the embeddings. The semantics of the
        network differ slightly in the two available training modes (CBOW or SG) but you can think of it as a NN with
        a single projection and hidden layer which we train on the corpus. The weights are then used as our embeddings
        (which means that the size of the hidden layer is equal to the number of features `self.size`).

    """

    def __init__(self, sentences=None, corpus_file=None, size=100, alpha=0.025, window=5, min_count=5,
                 max_vocab_size=None, sample=1e-3, seed=1, workers=3, min_alpha=0.0001,
                 sg=0, hs=0, negative=5, ns_exponent=0.75, cbow_mean=1, hashfxn=hash, iter=5, null_word=0,
                 trim_rule=None, sorted_vocab=1, batch_words=MAX_WORDS_IN_BATCH, compute_loss=False, callbacks=(),
                 max_final_vocab=None):
        """

        Parameters
        ----------
        sentences : iterable of iterables, optional
            The `sentences` iterable can be simply a list of lists of tokens, but for larger corpora,
            consider an iterable that streams the sentences directly from disk/network.
            See :class:`~gensim.models.word2vec.BrownCorpus`, :class:`~gensim.models.word2vec.Text8Corpus`
            or :class:`~gensim.models.word2vec.LineSentence` in :mod:`~gensim.models.word2vec` module for such examples.
            See also the `tutorial on data streaming in Python
            <https://rare-technologies.com/data-streaming-in-python-generators-iterators-iterables/>`_.
            If you don't supply `sentences`, the model is left uninitialized -- use if you plan to initialize it
            in some other way.
        corpus_file : str, optional
            Path to a corpus file in :class:`~gensim.models.word2vec.LineSentence` format.
            You may use this argument instead of `sentences` to get performance boost. Only one of `sentences` or
            `corpus_file` arguments need to be passed (or none of them, in that case, the model is left uninitialized).
        size : int, optional
            Dimensionality of the word vectors.
        window : int, optional
            Maximum distance between the current and predicted word within a sentence.
        min_count : int, optional
            Ignores all words with total frequency lower than this.
        workers : int, optional
            Use these many worker threads to train the model (=faster training with multicore machines).
        sg : {0, 1}, optional
            Training algorithm: 1 for skip-gram; otherwise CBOW.
        hs : {0, 1}, optional
            If 1, hierarchical softmax will be used for model training.
            If 0, and `negative` is non-zero, negative sampling will be used.
        negative : int, optional
            If > 0, negative sampling will be used, the int for negative specifies how many "noise words"
            should be drawn (usually between 5-20).
            If set to 0, no negative sampling is used.
        ns_exponent : float, optional
            The exponent used to shape the negative sampling distribution. A value of 1.0 samples exactly in proportion
            to the frequencies, 0.0 samples all words equally, while a negative value samples low-frequency words more
            than high-frequency words. The popular default value of 0.75 was chosen by the original Word2Vec paper.
            More recently, in https://arxiv.org/abs/1804.04212, Caselles-Dupré, Lesaint, & Royo-Letelier suggest that
            other values may perform better for recommendation applications.
        cbow_mean : {0, 1}, optional
            If 0, use the sum of the context word vectors. If 1, use the mean, only applies when cbow is used.
        alpha : float, optional
            The initial learning rate.
        min_alpha : float, optional
            Learning rate will linearly drop to `min_alpha` as training progresses.
        seed : int, optional
            Seed for the random number generator. Initial vectors for each word are seeded with a hash of
            the concatenation of word + `str(seed)`. Note that for a fully deterministically-reproducible run,
            you must also limit the model to a single worker thread (`workers=1`), to eliminate ordering jitter
            from OS thread scheduling. (In Python 3, reproducibility between interpreter launches also requires
            use of the `PYTHONHASHSEED` environment variable to control hash randomization).
        max_vocab_size : int, optional
            Limits the RAM during vocabulary building; if there are more unique
            words than this, then prune the infrequent ones. Every 10 million word types need about 1GB of RAM.
            Set to `None` for no limit.
        max_final_vocab : int, optional
            Limits the vocab to a target vocab size by automatically picking a matching min_count. If the specified
            min_count is more than the calculated min_count, the specified min_count will be used.
            Set to `None` if not required.
        sample : float, optional
            The threshold for configuring which higher-frequency words are randomly downsampled,
            useful range is (0, 1e-5).
        hashfxn : function, optional
            Hash function to use to randomly initialize weights, for increased training reproducibility.
        iter : int, optional
            Number of iterations (epochs) over the corpus.
        trim_rule : function, optional
            Vocabulary trimming rule, specifies whether certain words should remain in the vocabulary,
            be trimmed away, or handled using the default (discard if word count < min_count).
            Can be None (min_count will be used, look to :func:`~gensim.utils.keep_vocab_item`),
            or a callable that accepts parameters (word, count, min_count) and returns either
            :attr:`gensim.utils.RULE_DISCARD`, :attr:`gensim.utils.RULE_KEEP` or :attr:`gensim.utils.RULE_DEFAULT`.
            The rule, if given, is only used to prune vocabulary during build_vocab() and is not stored as part of the
            model.

            The input parameters are of the following types:
                * `word` (str) - the word we are examining
                * `count` (int) - the word's frequency count in the corpus
                * `min_count` (int) - the minimum count threshold.
        sorted_vocab : {0, 1}, optional
            If 1, sort the vocabulary by descending frequency before assigning word indexes.
            See :meth:`~gensim.models.word2vec.Word2VecVocab.sort_vocab()`.
        batch_words : int, optional
            Target size (in words) for batches of examples passed to worker threads (and
            thus cython routines).(Larger batches will be passed if individual
            texts are longer than 10000 words, but the standard cython code truncates to that maximum.)
        compute_loss: bool, optional
            If True, computes and stores loss value which can be retrieved using
            :meth:`~gensim.models.word2vec.Word2Vec.get_latest_training_loss`.
        callbacks : iterable of :class:`~gensim.models.callbacks.CallbackAny2Vec`, optional
            Sequence of callbacks to be executed at specific stages during training.

        Examples
        --------
        Initialize and train a :class:`~gensim.models.word2vec.Word2Vec` model

        .. sourcecode:: pycon

            >>> from gensim.models import Word2Vec
            >>> sentences = [["cat", "say", "meow"], ["dog", "say", "woof"]]
            >>> model = Word2Vec(sentences, min_count=1)

        """
        self.max_final_vocab = max_final_vocab

        self.callbacks = callbacks
        self.load = call_on_class_only

        self.wv = Word2VecKeyedVectors(size)
        self.vocabulary = Word2VecVocab(
            max_vocab_size=max_vocab_size, min_count=min_count, sample=sample, sorted_vocab=bool(sorted_vocab),
            null_word=null_word, max_final_vocab=max_final_vocab, ns_exponent=ns_exponent)
        self.trainables = Word2VecTrainables(seed=seed, vector_size=size, hashfxn=hashfxn)

        super(Word2Vec, self).__init__(
            sentences=sentences, corpus_file=corpus_file, workers=workers, vector_size=size, epochs=iter,
            callbacks=callbacks, batch_words=batch_words, trim_rule=trim_rule, sg=sg, alpha=alpha, window=window,
            seed=seed, hs=hs, negative=negative, cbow_mean=cbow_mean, min_alpha=min_alpha, compute_loss=compute_loss,
            fast_version=FAST_VERSION)

    def _do_train_epoch(self, corpus_file, thread_id, offset, cython_vocab, thread_private_mem, cur_epoch,
                        total_examples=None, total_words=None, **kwargs):
        work, neu1 = thread_private_mem

        if self.sg:
            examples, tally, raw_tally = train_epoch_sg(self, corpus_file, offset, cython_vocab, cur_epoch,
                                                        total_examples, total_words, work, neu1, self.compute_loss)
        else:
            examples, tally, raw_tally = train_epoch_cbow(self, corpus_file, offset, cython_vocab, cur_epoch,
                                                          total_examples, total_words, work, neu1, self.compute_loss)

        return examples, tally, raw_tally

    def _do_train_job(self, sentences, alpha, inits):
        """Train the model on a single batch of sentences.

        Parameters
        ----------
        sentences : iterable of list of str
            Corpus chunk to be used in this training batch.
        alpha : float
            The learning rate used in this batch.
        inits : (np.ndarray, np.ndarray)
            Each worker threads private work memory.

        Returns
        -------
        (int, int)
             2-tuple (effective word count after ignoring unknown words and sentence length trimming, total word count).

        """
        work, neu1 = inits
        tally = 0
        if self.sg:
            tally += train_batch_sg(self, sentences, alpha, work, self.compute_loss)
        else:
            tally += train_batch_cbow(self, sentences, alpha, work, neu1, self.compute_loss)
        return tally, self._raw_word_count(sentences)

    def _clear_post_train(self):
        """Remove all L2-normalized word vectors from the model."""
        self.wv.vectors_norm = None

    def _set_train_params(self, **kwargs):
        if 'compute_loss' in kwargs:
            self.compute_loss = kwargs['compute_loss']
        self.running_training_loss = 0

    def train(self, sentences=None, corpus_file=None, total_examples=None, total_words=None,
              epochs=None, start_alpha=None, end_alpha=None, word_count=0,
              queue_factor=2, report_delay=1.0, compute_loss=False, callbacks=()):
        """Update the model's neural weights from a sequence of sentences.

        Notes
        -----
        To support linear learning-rate decay from (initial) `alpha` to `min_alpha`, and accurate
        progress-percentage logging, either `total_examples` (count of sentences) or `total_words` (count of
        raw words in sentences) **MUST** be provided. If `sentences` is the same corpus
        that was provided to :meth:`~gensim.models.word2vec.Word2Vec.build_vocab` earlier,
        you can simply use `total_examples=self.corpus_count`.

        Warnings
        --------
        To avoid common mistakes around the model's ability to do multiple training passes itself, an
        explicit `epochs` argument **MUST** be provided. In the common and recommended case
        where :meth:`~gensim.models.word2vec.Word2Vec.train` is only called once, you can set `epochs=self.iter`.

        Parameters
        ----------
        sentences : iterable of list of str
            The `sentences` iterable can be simply a list of lists of tokens, but for larger corpora,
            consider an iterable that streams the sentences directly from disk/network.
            See :class:`~gensim.models.word2vec.BrownCorpus`, :class:`~gensim.models.word2vec.Text8Corpus`
            or :class:`~gensim.models.word2vec.LineSentence` in :mod:`~gensim.models.word2vec` module for such examples.
            See also the `tutorial on data streaming in Python
            <https://rare-technologies.com/data-streaming-in-python-generators-iterators-iterables/>`_.
        corpus_file : str, optional
            Path to a corpus file in :class:`~gensim.models.word2vec.LineSentence` format.
            You may use this argument instead of `sentences` to get performance boost. Only one of `sentences` or
            `corpus_file` arguments need to be passed (not both of them).
        total_examples : int
            Count of sentences.
        total_words : int
            Count of raw words in sentences.
        epochs : int
            Number of iterations (epochs) over the corpus.
        start_alpha : float, optional
            Initial learning rate. If supplied, replaces the starting `alpha` from the constructor,
            for this one call to`train()`.
            Use only if making multiple calls to `train()`, when you want to manage the alpha learning-rate yourself
            (not recommended).
        end_alpha : float, optional
            Final learning rate. Drops linearly from `start_alpha`.
            If supplied, this replaces the final `min_alpha` from the constructor, for this one call to `train()`.
            Use only if making multiple calls to `train()`, when you want to manage the alpha learning-rate yourself
            (not recommended).
        word_count : int, optional
            Count of words already trained. Set this to 0 for the usual
            case of training on all words in sentences.
        queue_factor : int, optional
            Multiplier for size of queue (number of workers * queue_factor).
        report_delay : float, optional
            Seconds to wait before reporting progress.
        compute_loss: bool, optional
            If True, computes and stores loss value which can be retrieved using
            :meth:`~gensim.models.word2vec.Word2Vec.get_latest_training_loss`.
        callbacks : iterable of :class:`~gensim.models.callbacks.CallbackAny2Vec`, optional
            Sequence of callbacks to be executed at specific stages during training.

        Examples
        --------
        .. sourcecode:: pycon

            >>> from gensim.models import Word2Vec
            >>> sentences = [["cat", "say", "meow"], ["dog", "say", "woof"]]
            >>>
            >>> model = Word2Vec(min_count=1)
            >>> model.build_vocab(sentences)  # prepare the model vocabulary
            >>> model.train(sentences, total_examples=model.corpus_count, epochs=model.iter)  # train word vectors
            (1, 30)

        """
        return super(Word2Vec, self).train(
            sentences=sentences, corpus_file=corpus_file, total_examples=total_examples, total_words=total_words,
            epochs=epochs, start_alpha=start_alpha, end_alpha=end_alpha, word_count=word_count,
            queue_factor=queue_factor, report_delay=report_delay, compute_loss=compute_loss, callbacks=callbacks)

    def clear_sims(self):
        """Remove all L2-normalized word vectors from the model, to free up memory.

        You can recompute them later again using the :meth:`~gensim.models.word2vec.Word2Vec.init_sims` method.

        """
        self.wv.vectors_norm = None

    def intersect_word2vec_format(self, fname, lockf=0.0, binary=False, encoding='utf8', unicode_errors='strict'):
        """Merge in an input-hidden weight matrix loaded from the original C word2vec-tool format,
        where it intersects with the current vocabulary.

        No words are added to the existing vocabulary, but intersecting words adopt the file's weights, and
        non-intersecting words are left alone.

        Parameters
        ----------
        fname : str
            The file path to load the vectors from.
        lockf : float, optional
            Lock-factor value to be set for any imported word-vectors; the
            default value of 0.0 prevents further updating of the vector during subsequent
            training. Use 1.0 to allow further training updates of merged vectors.
        binary : bool, optional
            If True, `fname` is in the binary word2vec C format.
        encoding : str, optional
            Encoding of `text` for `unicode` function (python2 only).
        unicode_errors : str, optional
            Error handling behaviour, used as parameter for `unicode` function (python2 only).

        """
        overlap_count = 0
        logger.info("loading projection weights from %s", fname)
        with utils.smart_open(fname) as fin:
            header = utils.to_unicode(fin.readline(), encoding=encoding)
            vocab_size, vector_size = (int(x) for x in header.split())  # throws for invalid file format
            if not vector_size == self.wv.vector_size:
                raise ValueError("incompatible vector size %d in file %s" % (vector_size, fname))
                # TOCONSIDER: maybe mismatched vectors still useful enough to merge (truncating/padding)?
            if binary:
                binary_len = dtype(REAL).itemsize * vector_size
                for _ in range(vocab_size):
                    # mixed text and binary: read text first, then binary
                    word = []
                    while True:
                        ch = fin.read(1)
                        if ch == b' ':
                            break
                        if ch != b'\n':  # ignore newlines in front of words (some binary files have)
                            word.append(ch)
                    word = utils.to_unicode(b''.join(word), encoding=encoding, errors=unicode_errors)
                    weights = fromstring(fin.read(binary_len), dtype=REAL)
                    if word in self.wv.vocab:
                        overlap_count += 1
                        self.wv.vectors[self.wv.vocab[word].index] = weights
                        self.trainables.vectors_lockf[self.wv.vocab[word].index] = lockf  # lock-factor: 0.0=no changes
            else:
                for line_no, line in enumerate(fin):
                    parts = utils.to_unicode(line.rstrip(), encoding=encoding, errors=unicode_errors).split(" ")
                    if len(parts) != vector_size + 1:
                        raise ValueError("invalid vector on line %s (is this really the text format?)" % line_no)
                    word, weights = parts[0], [REAL(x) for x in parts[1:]]
                    if word in self.wv.vocab:
                        overlap_count += 1
                        self.wv.vectors[self.wv.vocab[word].index] = weights
                        self.trainables.vectors_lockf[self.wv.vocab[word].index] = lockf  # lock-factor: 0.0=no changes
        logger.info("merged %d vectors into %s matrix from %s", overlap_count, self.wv.vectors.shape, fname)

    @deprecated("Method will be removed in 4.0.0, use self.wv.__getitem__() instead")
    def __getitem__(self, words):
        """Deprecated. Use `self.wv.__getitem__` instead.
        Refer to the documentation for :meth:`~gensim.models.keyedvectors.Word2VecKeyedVectors.__getitem__`.

        """
        return self.wv.__getitem__(words)

    @deprecated("Method will be removed in 4.0.0, use self.wv.__contains__() instead")
    def __contains__(self, word):
        """Deprecated. Use `self.wv.__contains__` instead.
        Refer to the documentation for :meth:`~gensim.models.keyedvectors.Word2VecKeyedVectors.__contains__`.

        """
        return self.wv.__contains__(word)

    def predict_output_word(self, context_words_list, topn=10):
        """Get the probability distribution of the center word given context words.

        Parameters
        ----------
        context_words_list : list of str
            List of context words.
        topn : int, optional
            Return `topn` words and their probabilities.

        Returns
        -------
        list of (str, float)
            `topn` length list of tuples of (word, probability).

        """
        if not self.negative:
            raise RuntimeError(
                "We have currently only implemented predict_output_word for the negative sampling scheme, "
                "so you need to have run word2vec with negative > 0 for this to work."
            )

        if not hasattr(self.wv, 'vectors') or not hasattr(self.trainables, 'syn1neg'):
            raise RuntimeError("Parameters required for predicting the output words not found.")

        word_vocabs = [self.wv.vocab[w] for w in context_words_list if w in self.wv.vocab]
        if not word_vocabs:
            warnings.warn("All the input context words are out-of-vocabulary for the current model.")
            return None

        word2_indices = [word.index for word in word_vocabs]

        l1 = np_sum(self.wv.vectors[word2_indices], axis=0)
        if word2_indices and self.cbow_mean:
            l1 /= len(word2_indices)

        # propagate hidden -> output and take softmax to get probabilities
        prob_values = exp(dot(l1, self.trainables.syn1neg.T))
        prob_values /= sum(prob_values)
        top_indices = matutils.argsort(prob_values, topn=topn, reverse=True)
        # returning the most probable output words with their probabilities
        return [(self.wv.index2word[index1], prob_values[index1]) for index1 in top_indices]

    def init_sims(self, replace=False):
        """Deprecated. Use `self.wv.init_sims` instead.
        See :meth:`~gensim.models.keyedvectors.Word2VecKeyedVectors.init_sims`.

        """
        if replace and hasattr(self.trainables, 'syn1'):
            del self.trainables.syn1
        return self.wv.init_sims(replace)

    def reset_from(self, other_model):
        """Borrow shareable pre-built structures from `other_model` and reset hidden layer weights.

        Structures copied are:
            * Vocabulary
            * Index to word mapping
            * Cumulative frequency table (used for negative sampling)
            * Cached corpus length

        Useful when testing multiple models on the same corpus in parallel.

        Parameters
        ----------
        other_model : :class:`~gensim.models.word2vec.Word2Vec`
            Another model to copy the internal structures from.

        """
        self.wv.vocab = other_model.wv.vocab
        self.wv.index2word = other_model.wv.index2word
        self.vocabulary.cum_table = other_model.vocabulary.cum_table
        self.corpus_count = other_model.corpus_count
        self.trainables.reset_weights(self.hs, self.negative, self.wv)

    @staticmethod
    def log_accuracy(section):
        """Deprecated. Use `self.wv.log_accuracy` instead.
        See :meth:`~gensim.models.word2vec.Word2VecKeyedVectors.log_accuracy`.

        """
        return Word2VecKeyedVectors.log_accuracy(section)

    @deprecated("Method will be removed in 4.0.0, use self.wv.evaluate_word_analogies() instead")
    def accuracy(self, questions, restrict_vocab=30000, most_similar=None, case_insensitive=True):
        """Deprecated. Use `self.wv.accuracy` instead.
        See :meth:`~gensim.models.word2vec.Word2VecKeyedVectors.accuracy`.

        """
        most_similar = most_similar or Word2VecKeyedVectors.most_similar
        return self.wv.accuracy(questions, restrict_vocab, most_similar, case_insensitive)

    def __str__(self):
        """Human readable representation of the model's state.

        Returns
        -------
        str
            Human readable representation of the model's state, including the vocabulary size, vector size
            and learning rate.

        """
        return "%s(vocab=%s, size=%s, alpha=%s)" % (
            self.__class__.__name__, len(self.wv.index2word), self.wv.vector_size, self.alpha
        )

    def delete_temporary_training_data(self, replace_word_vectors_with_normalized=False):
        """Discard parameters that are used in training and scoring, to save memory.

        Warnings
        --------
        Use only if you're sure you're done training a model.

        Parameters
        ----------
        replace_word_vectors_with_normalized : bool, optional
            If True, forget the original (not normalized) word vectors and only keep
            the L2-normalized word vectors, to save even more memory.

        """
        if replace_word_vectors_with_normalized:
            self.init_sims(replace=True)
        self._minimize_model()

    def save(self, *args, **kwargs):
        """Save the model.
        This saved model can be loaded again using :func:`~gensim.models.word2vec.Word2Vec.load`, which supports
        online training and getting vectors for vocabulary words.

        Parameters
        ----------
        fname : str
            Path to the file.

        """
        # don't bother storing the cached normalized vectors, recalculable table
        kwargs['ignore'] = kwargs.get('ignore', ['vectors_norm', 'cum_table'])
        super(Word2Vec, self).save(*args, **kwargs)

    def get_latest_training_loss(self):
        """Get current value of the training loss.

        Returns
        -------
        float
            Current training loss.

        """
        return self.running_training_loss

    @deprecated(
        "Method will be removed in 4.0.0, keep just_word_vectors = model.wv to retain just the KeyedVectors instance"
    )
    def _minimize_model(self, save_syn1=False, save_syn1neg=False, save_vectors_lockf=False):
        if save_syn1 and save_syn1neg and save_vectors_lockf:
            return
        if hasattr(self.trainables, 'syn1') and not save_syn1:
            del self.trainables.syn1
        if hasattr(self.trainables, 'syn1neg') and not save_syn1neg:
            del self.trainables.syn1neg
        if hasattr(self.trainables, 'vectors_lockf') and not save_vectors_lockf:
            del self.trainables.vectors_lockf
        self.model_trimmed_post_training = True

    @classmethod
    def load_word2vec_format(
            cls, fname, fvocab=None, binary=False, encoding='utf8', unicode_errors='strict',
            limit=None, datatype=REAL):
        """Deprecated. Use :meth:`gensim.models.KeyedVectors.load_word2vec_format` instead."""
        raise DeprecationWarning("Deprecated. Use gensim.models.KeyedVectors.load_word2vec_format instead.")

    def save_word2vec_format(self, fname, fvocab=None, binary=False):
        """Deprecated. Use `model.wv.save_word2vec_format` instead.
        See :meth:`gensim.models.KeyedVectors.save_word2vec_format`.

        """
        raise DeprecationWarning("Deprecated. Use model.wv.save_word2vec_format instead.")

    @classmethod
    def load(cls, *args, **kwargs):
        """Load a previously saved :class:`~gensim.models.word2vec.Word2Vec` model.

        See Also
        --------
        :meth:`~gensim.models.word2vec.Word2Vec.save`
            Save model.

        Parameters
        ----------
        fname : str
            Path to the saved file.

        Returns
        -------
        :class:`~gensim.models.word2vec.Word2Vec`
            Loaded model.

        """
        try:
            model = super(Word2Vec, cls).load(*args, **kwargs)

            # for backward compatibility for `max_final_vocab` feature
            if not hasattr(model, 'max_final_vocab'):
                model.max_final_vocab = None
                model.vocabulary.max_final_vocab = None

            return model
        except AttributeError:
            logger.info('Model saved using code from earlier Gensim Version. Re-loading old model in a compatible way.')
            from gensim.models.deprecated.word2vec import load_old_word2vec
            return load_old_word2vec(*args, **kwargs)


class LineSentence(object):
    """Iterate over a file that contains sentences: one line = one sentence.
    Words must be already preprocessed and separated by whitespace.

    """
    def __init__(self, source, max_sentence_length=MAX_WORDS_IN_BATCH, limit=None):
        """

        Parameters
        ----------
        source : string or a file-like object
            Path to the file on disk, or an already-open file object (must support `seek(0)`).
        limit : int or None
            Clip the file to the first `limit` lines. Do no clipping if `limit is None` (the default).

        Examples
        --------
        .. sourcecode:: pycon

            >>> from gensim.test.utils import datapath
            >>> sentences = LineSentence(datapath('lee_background.cor'))
            >>> for sentence in sentences:
            ...     pass

        """
        self.source = source
        self.max_sentence_length = max_sentence_length
        self.limit = limit

    def __iter__(self):
        """Iterate through the lines in the source."""
        try:
            # Assume it is a file-like object and try treating it as such
            # Things that don't have seek will trigger an exception
            self.source.seek(0)
            for line in itertools.islice(self.source, self.limit):
                line = utils.to_unicode(line).split()
                i = 0
                while i < len(line):
                    yield line[i: i + self.max_sentence_length]
                    i += self.max_sentence_length
        except AttributeError:
            # If it didn't work like a file, use it as a string filename
            with utils.smart_open(self.source) as fin:
                for line in itertools.islice(fin, self.limit):
                    line = utils.to_unicode(line).split()
                    i = 0
                    while i < len(line):
                        yield line[i: i + self.max_sentence_length]
                        i += self.max_sentence_length


def _scan_vocab_worker(stream, progress_queue, max_vocab_size=None, trim_rule=None):
    """Do an initial scan of all words appearing in stream.

    Note: This function can not be Word2VecVocab's method because
    of multiprocessing synchronization specifics in Python.
    """
    min_reduce = 1
    vocab = defaultdict(int)
    checked_string_types = 0
    sentence_no = -1
    total_words = 0
    for sentence_no, sentence in enumerate(stream):
        if not checked_string_types:
            if isinstance(sentence, string_types):
                log_msg = "Each 'sentences' item should be a list of words (usually unicode strings). " \
                          "First item here is instead plain %s." % type(sentence)
                progress_queue.put(log_msg)

            checked_string_types += 1

        for word in sentence:
            vocab[word] += 1

        if max_vocab_size and len(vocab) > max_vocab_size:
            utils.prune_vocab(vocab, min_reduce, trim_rule=trim_rule)
            min_reduce += 1

        total_words += len(sentence)

    progress_queue.put((total_words, sentence_no + 1))
    progress_queue.put(None)
    return vocab


class Word2VecVocab(utils.SaveLoad):
    """Vocabulary used by :class:`~gensim.models.word2vec.Word2Vec`."""
    def __init__(
            self, max_vocab_size=None, min_count=5, sample=1e-3, sorted_vocab=True, null_word=0,
            max_final_vocab=None, ns_exponent=0.75):
        self.max_vocab_size = max_vocab_size
        self.min_count = min_count
        self.sample = sample
        self.sorted_vocab = sorted_vocab
        self.null_word = null_word
        self.cum_table = None  # for negative sampling
        self.raw_vocab = None
        self.max_final_vocab = max_final_vocab
        self.ns_exponent = ns_exponent

    def _scan_vocab(self, sentences, progress_per, trim_rule):
        sentence_no = -1
        total_words = 0
        min_reduce = 1
        vocab = defaultdict(int)
        checked_string_types = 0
        for sentence_no, sentence in enumerate(sentences):
            if not checked_string_types:
                if isinstance(sentence, string_types):
                    logger.warning(
                        "Each 'sentences' item should be a list of words (usually unicode strings). "
                        "First item here is instead plain %s.",
                        type(sentence)
                    )
                checked_string_types += 1
            if sentence_no % progress_per == 0:
                logger.info(
                    "PROGRESS: at sentence #%i, processed %i words, keeping %i word types",
                    sentence_no, total_words, len(vocab)
                )
            for word in sentence:
                vocab[word] += 1
            total_words += len(sentence)

            if self.max_vocab_size and len(vocab) > self.max_vocab_size:
                utils.prune_vocab(vocab, min_reduce, trim_rule=trim_rule)
                min_reduce += 1

        corpus_count = sentence_no + 1
        self.raw_vocab = vocab
        return total_words, corpus_count

    def scan_vocab(self, sentences=None, corpus_file=None, progress_per=10000, workers=None, trim_rule=None):
        logger.info("collecting all words and their counts")
        if corpus_file:
            sentences = LineSentence(corpus_file)

        total_words, corpus_count = self._scan_vocab(sentences, progress_per, trim_rule)

        logger.info(
            "collected %i word types from a corpus of %i raw words and %i sentences",
            len(self.raw_vocab), total_words, corpus_count
        )

        return total_words, corpus_count

    def sort_vocab(self, wv):
        """Sort the vocabulary so the most frequent words have the lowest indexes."""
        if len(wv.vectors):
            raise RuntimeError("cannot sort vocabulary after model weights already initialized.")
        wv.index2word.sort(key=lambda word: wv.vocab[word].count, reverse=True)
        for i, word in enumerate(wv.index2word):
            wv.vocab[word].index = i

    def prepare_vocab(
            self, hs, negative, wv, update=False, keep_raw_vocab=False, trim_rule=None,
            min_count=None, sample=None, dry_run=False):
        """Apply vocabulary settings for `min_count` (discarding less-frequent words)
        and `sample` (controlling the downsampling of more-frequent words).

        Calling with `dry_run=True` will only simulate the provided settings and
        report the size of the retained vocabulary, effective corpus length, and
        estimated memory requirements. Results are both printed via logging and
        returned as a dict.

        Delete the raw vocabulary after the scaling is done to free up RAM,
        unless `keep_raw_vocab` is set.

        """
        min_count = min_count or self.min_count
        sample = sample or self.sample
        drop_total = drop_unique = 0

        # set effective_min_count to min_count in case max_final_vocab isn't set
        self.effective_min_count = min_count

        # if max_final_vocab is specified instead of min_count
        # pick a min_count which satisfies max_final_vocab as well as possible
        if self.max_final_vocab is not None:
            sorted_vocab = sorted(self.raw_vocab.keys(), key=lambda word: self.raw_vocab[word], reverse=True)
            calc_min_count = 1

            if self.max_final_vocab < len(sorted_vocab):
                calc_min_count = self.raw_vocab[sorted_vocab[self.max_final_vocab]] + 1

            self.effective_min_count = max(calc_min_count, min_count)
            logger.info(
                "max_final_vocab=%d and min_count=%d resulted in calc_min_count=%d, effective_min_count=%d",
                self.max_final_vocab, min_count, calc_min_count, self.effective_min_count
            )

        if not update:
            logger.info("Loading a fresh vocabulary")
            retain_total, retain_words = 0, []
            # Discard words less-frequent than min_count
            if not dry_run:
                wv.index2word = []
                # make stored settings match these applied settings
                self.min_count = min_count
                self.sample = sample
                wv.vocab = {}

            for word, v in iteritems(self.raw_vocab):
                if keep_vocab_item(word, v, self.effective_min_count, trim_rule=trim_rule):
                    retain_words.append(word)
                    retain_total += v
                    if not dry_run:
                        wv.vocab[word] = Vocab(count=v, index=len(wv.index2word))
                        wv.index2word.append(word)
                else:
                    drop_unique += 1
                    drop_total += v
            original_unique_total = len(retain_words) + drop_unique
            retain_unique_pct = len(retain_words) * 100 / max(original_unique_total, 1)
            logger.info(
                "effective_min_count=%d retains %i unique words (%i%% of original %i, drops %i)",
                self.effective_min_count, len(retain_words), retain_unique_pct, original_unique_total, drop_unique
            )
            original_total = retain_total + drop_total
            retain_pct = retain_total * 100 / max(original_total, 1)
            logger.info(
                "effective_min_count=%d leaves %i word corpus (%i%% of original %i, drops %i)",
                self.effective_min_count, retain_total, retain_pct, original_total, drop_total
            )
        else:
            logger.info("Updating model with new vocabulary")
            new_total = pre_exist_total = 0
            new_words = pre_exist_words = []
            for word, v in iteritems(self.raw_vocab):
                if keep_vocab_item(word, v, self.effective_min_count, trim_rule=trim_rule):
                    if word in wv.vocab:
                        pre_exist_words.append(word)
                        pre_exist_total += v
                        if not dry_run:
                            wv.vocab[word].count += v
                    else:
                        new_words.append(word)
                        new_total += v
                        if not dry_run:
                            wv.vocab[word] = Vocab(count=v, index=len(wv.index2word))
                            wv.index2word.append(word)
                else:
                    drop_unique += 1
                    drop_total += v
            original_unique_total = len(pre_exist_words) + len(new_words) + drop_unique
            pre_exist_unique_pct = len(pre_exist_words) * 100 / max(original_unique_total, 1)
            new_unique_pct = len(new_words) * 100 / max(original_unique_total, 1)
            logger.info(
                "New added %i unique words (%i%% of original %i) "
                "and increased the count of %i pre-existing words (%i%% of original %i)",
                len(new_words), new_unique_pct, original_unique_total, len(pre_exist_words),
                pre_exist_unique_pct, original_unique_total
            )
            retain_words = new_words + pre_exist_words
            retain_total = new_total + pre_exist_total

        # Precalculate each vocabulary item's threshold for sampling
        if not sample:
            # no words downsampled
            threshold_count = retain_total
        elif sample < 1.0:
            # traditional meaning: set parameter as proportion of total
            threshold_count = sample * retain_total
        else:
            # new shorthand: sample >= 1 means downsample all words with higher count than sample
            threshold_count = int(sample * (3 + sqrt(5)) / 2)

        downsample_total, downsample_unique = 0, 0
        for w in retain_words:
            v = self.raw_vocab[w]
            word_probability = (sqrt(v / threshold_count) + 1) * (threshold_count / v)
            if word_probability < 1.0:
                downsample_unique += 1
                downsample_total += word_probability * v
            else:
                word_probability = 1.0
                downsample_total += v
            if not dry_run:
                wv.vocab[w].sample_int = int(round(word_probability * 2**32))

        if not dry_run and not keep_raw_vocab:
            logger.info("deleting the raw counts dictionary of %i items", len(self.raw_vocab))
            self.raw_vocab = defaultdict(int)

        logger.info("sample=%g downsamples %i most-common words", sample, downsample_unique)
        logger.info(
            "downsampling leaves estimated %i word corpus (%.1f%% of prior %i)",
            downsample_total, downsample_total * 100.0 / max(retain_total, 1), retain_total
        )

        # return from each step: words-affected, resulting-corpus-size, extra memory estimates
        report_values = {
            'drop_unique': drop_unique, 'retain_total': retain_total, 'downsample_unique': downsample_unique,
            'downsample_total': int(downsample_total), 'num_retained_words': len(retain_words)
        }

        if self.null_word:
            # create null pseudo-word for padding when using concatenative L1 (run-of-words)
            # this word is only ever input – never predicted – so count, huffman-point, etc doesn't matter
            self.add_null_word(wv)

        if self.sorted_vocab and not update:
            self.sort_vocab(wv)
        if hs:
            # add info about each word's Huffman encoding
            self.create_binary_tree(wv)
        if negative:
            # build the table for drawing random words (for negative sampling)
            self.make_cum_table(wv)

        return report_values

    def add_null_word(self, wv):
        word, v = '\0', Vocab(count=1, sample_int=0)
        v.index = len(wv.vocab)
        wv.index2word.append(word)
        wv.vocab[word] = v

    def create_binary_tree(self, wv):
        """Create a `binary Huffman tree <https://en.wikipedia.org/wiki/Huffman_coding>`_ using stored vocabulary
        word counts. Frequent words will have shorter binary codes.
        Called internally from :meth:`~gensim.models.word2vec.Word2VecVocab.build_vocab`.

        """
        _assign_binary_codes(wv.vocab)

    def make_cum_table(self, wv, domain=2**31 - 1):
        """Create a cumulative-distribution table using stored vocabulary word counts for
        drawing random words in the negative-sampling training routines.

        To draw a word index, choose a random integer up to the maximum value in the table (cum_table[-1]),
        then finding that integer's sorted insertion point (as if by `bisect_left` or `ndarray.searchsorted()`).
        That insertion point is the drawn index, coming up in proportion equal to the increment at that slot.

        Called internally from :meth:`~gensim.models.word2vec.Word2VecVocab.build_vocab`.

        """
        vocab_size = len(wv.index2word)
        self.cum_table = zeros(vocab_size, dtype=uint32)
        # compute sum of all power (Z in paper)
        train_words_pow = 0.0
        for word_index in range(vocab_size):
            train_words_pow += wv.vocab[wv.index2word[word_index]].count**self.ns_exponent
        cumulative = 0.0
        for word_index in range(vocab_size):
            cumulative += wv.vocab[wv.index2word[word_index]].count**self.ns_exponent
            self.cum_table[word_index] = round(cumulative / train_words_pow * domain)
        if len(self.cum_table) > 0:
            assert self.cum_table[-1] == domain


def _build_heap(vocab):
    heap = list(itervalues(vocab))
    heapq.heapify(heap)
    for i in range(len(vocab) - 1):
        min1, min2 = heapq.heappop(heap), heapq.heappop(heap)
        heapq.heappush(
            heap, Vocab(count=min1.count + min2.count, index=i + len(vocab), left=min1, right=min2)
        )
    return heap


def _assign_binary_codes(vocab):
    """
    Appends a binary code to each vocab term.

    Parameters
    ----------
    vocab : dict
        A dictionary of :class:`gensim.models.word2vec.Vocab` objects.

    Notes
    -----
    Expects each term to have an .index attribute that contains the order in
    which the term was added to the vocabulary.  E.g. term.index == 0 means the
    term was added to the vocab first.

    Sets the .code and .point attributes of each node.
    Each code is a numpy.array containing 0s and 1s.
    Each point is an integer.

    """
    logger.info("constructing a huffman tree from %i words", len(vocab))

    heap = _build_heap(vocab)
    if not heap:
        #
        # TODO: how can we end up with an empty heap?
        #
        logger.info("built huffman tree with maximum node depth 0")
        return

    # recurse over the tree, assigning a binary code to each vocabulary word
    max_depth = 0
    stack = [(heap[0], [], [])]
    while stack:
        node, codes, points = stack.pop()
        if node.index < len(vocab):
            # leaf node => store its path from the root
            node.code, node.point = codes, points
            max_depth = max(len(codes), max_depth)
        else:
            # inner node => continue recursion
            points = array(list(points) + [node.index - len(vocab)], dtype=uint32)
            stack.append((node.left, array(list(codes) + [0], dtype=uint8), points))
            stack.append((node.right, array(list(codes) + [1], dtype=uint8), points))

    logger.info("built huffman tree with maximum node depth %i", max_depth)


class Word2VecTrainables(utils.SaveLoad):
    """Represents the inner shallow neural network used to train :class:`~gensim.models.word2vec.Word2Vec`."""
    def __init__(self, vector_size=100, seed=1, hashfxn=hash):
        self.hashfxn = hashfxn
        self.layer1_size = vector_size
        self.seed = seed

    def prepare_weights(self, hs, negative, wv, update=False, vocabulary=None):
        """Build tables and model weights based on final vocabulary settings."""
        # set initial input/projection and hidden weights
        if not update:
            self.reset_weights(hs, negative, wv)
        else:
            self.update_weights(hs, negative, wv)

    def seeded_vector(self, seed_string, vector_size):
        """Get a random vector (but deterministic by seed_string)."""
        # Note: built-in hash() may vary by Python version or even (in Py3.x) per launch
        once = random.RandomState(self.hashfxn(seed_string) & 0xffffffff)
        return (once.rand(vector_size) - 0.5) / vector_size

    def reset_weights(self, hs, negative, wv):
        """Reset all projection weights to an initial (untrained) state, but keep the existing vocabulary."""
        logger.info("resetting layer weights")
        wv.vectors = empty((len(wv.vocab), wv.vector_size), dtype=REAL)
        # randomize weights vector by vector, rather than materializing a huge random matrix in RAM at once
        for i in range(len(wv.vocab)):
            # construct deterministic seed from word AND seed argument
            wv.vectors[i] = self.seeded_vector(wv.index2word[i] + str(self.seed), wv.vector_size)
        if hs:
            self.syn1 = zeros((len(wv.vocab), self.layer1_size), dtype=REAL)
        if negative:
            self.syn1neg = zeros((len(wv.vocab), self.layer1_size), dtype=REAL)
        wv.vectors_norm = None

        self.vectors_lockf = ones(len(wv.vocab), dtype=REAL)  # zeros suppress learning

    def update_weights(self, hs, negative, wv):
        """Copy all the existing weights, and reset the weights for the newly added vocabulary."""
        logger.info("updating layer weights")
        gained_vocab = len(wv.vocab) - len(wv.vectors)
        newvectors = empty((gained_vocab, wv.vector_size), dtype=REAL)

        # randomize the remaining words
        for i in range(len(wv.vectors), len(wv.vocab)):
            # construct deterministic seed from word AND seed argument
            newvectors[i - len(wv.vectors)] = self.seeded_vector(wv.index2word[i] + str(self.seed), wv.vector_size)

        # Raise an error if an online update is run before initial training on a corpus
        if not len(wv.vectors):
            raise RuntimeError(
                "You cannot do an online vocabulary-update of a model which has no prior vocabulary. "
                "First build the vocabulary of your model with a corpus before doing an online update."
            )

        wv.vectors = vstack([wv.vectors, newvectors])

        if hs:
            self.syn1 = vstack([self.syn1, zeros((gained_vocab, self.layer1_size), dtype=REAL)])
        if negative:
            pad = zeros((gained_vocab, self.layer1_size), dtype=REAL)
            self.syn1neg = vstack([self.syn1neg, pad])
        wv.vectors_norm = None

        # do not suppress learning for already learned words
        self.vectors_lockf = ones(len(wv.vocab), dtype=REAL)  # zeros suppress learning


# Example: ./word2vec.py -train data.txt -output vec.txt -size 200 -window 5 -sample 1e-4 \
# -negative 5 -hs 0 -binary 0 -cbow 1 -iter 3
if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        format='%(asctime)s : %(threadName)s : %(levelname)s : %(message)s',
        level=logging.INFO
    )
    logger.info("running %s", " ".join(sys.argv))
    logger.info("using optimization %s", FAST_VERSION)

    # check and process cmdline input
    program = os.path.basename(sys.argv[0])
    if len(sys.argv) < 2:
        print(globals()['__doc__'] % locals())
        sys.exit(1)

    from gensim.models.word2vec import Word2Vec  # noqa:F811 avoid referencing __main__ in pickle

    seterr(all='raise')  # don't ignore numpy errors

    parser = argparse.ArgumentParser()
    parser.add_argument("-train", help="Use text data from file TRAIN to train the model", required=True)
    parser.add_argument("-output", help="Use file OUTPUT to save the resulting word vectors")
    parser.add_argument("-window", help="Set max skip length WINDOW between words; default is 5", type=int, default=5)
    parser.add_argument("-size", help="Set size of word vectors; default is 100", type=int, default=100)
    parser.add_argument(
        "-sample",
        help="Set threshold for occurrence of words. "
             "Those that appear with higher frequency in the training data will be randomly down-sampled;"
             " default is 1e-3, useful range is (0, 1e-5)",
        type=float, default=1e-3
    )
    parser.add_argument(
        "-hs", help="Use Hierarchical Softmax; default is 0 (not used)",
        type=int, default=0, choices=[0, 1]
    )
    parser.add_argument(
        "-negative", help="Number of negative examples; default is 5, common values are 3 - 10 (0 = not used)",
        type=int, default=5
    )
    parser.add_argument("-threads", help="Use THREADS threads (default 12)", type=int, default=12)
    parser.add_argument("-iter", help="Run more training iterations (default 5)", type=int, default=5)
    parser.add_argument(
        "-min_count", help="This will discard words that appear less than MIN_COUNT times; default is 5",
        type=int, default=5
    )
    parser.add_argument(
        "-cbow", help="Use the continuous bag of words model; default is 1 (use 0 for skip-gram model)",
        type=int, default=1, choices=[0, 1]
    )
    parser.add_argument(
        "-binary", help="Save the resulting vectors in binary mode; default is 0 (off)",
        type=int, default=0, choices=[0, 1]
    )
    parser.add_argument("-accuracy", help="Use questions from file ACCURACY to evaluate the model")

    args = parser.parse_args()

    if args.cbow == 0:
        skipgram = 1
    else:
        skipgram = 0

    corpus = LineSentence(args.train)

    model = Word2Vec(
        corpus, size=args.size, min_count=args.min_count, workers=args.threads,
        window=args.window, sample=args.sample, sg=skipgram, hs=args.hs,
        negative=args.negative, cbow_mean=1, iter=args.iter
    )

    if args.output:
        outfile = args.output
        model.wv.save_word2vec_format(outfile, binary=args.binary)
    else:
        outfile = args.train
        model.save(outfile + '.model')
    if args.binary == 1:
        model.wv.save_word2vec_format(outfile + '.model.bin', binary=True)
    else:
        model.wv.save_word2vec_format(outfile + '.model.txt', binary=False)

    if args.accuracy:
        model.accuracy(args.accuracy)

    logger.info("finished running %s", program)