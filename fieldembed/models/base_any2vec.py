#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Author: Shiva Manne <manneshiva@gmail.com>
# Copyright (C) 2018 RaRe Technologies s.r.o.
# Licensed under the GNU LGPL v2.1 - http://www.gnu.org/licenses/lgpl.html

r"""This module contains base classes required for implementing \*2vec algorithms.

The class hierarchy is designed to facilitate adding more concrete implementations for creating embeddings.
In the most general case, the purpose of this class is to transform an arbitrary representation to a numerical vector
(embedding). This is represented by the base :class:`~gensim.models.base_any2vec.BaseAny2VecModel`. The input space in
most cases (in the NLP field at least) is plain text. For this reason, we enrich the class hierarchy with the abstract
:class:`~gensim.models.base_any2vec.BaseWordEmbeddingsModel` to be used as a base for models where the input
space is text.

Notes
-----
Even though this is the usual case, not all embeddings transform text, such as the
:class:`~gensim.models.poincare.PoincareModel` that embeds graphs.

See Also
--------
:class:`~gensim.models.word2vec.Word2Vec`.
    Word2Vec model - embeddings for words.
:class:`~gensim.models.fasttext.FastText`.
    FastText model - embeddings for words (ngram-based).
:class:`~gensim.models.doc2vec.Doc2Vec`.
    Doc2Vec model - embeddings for documents.
:class:`~gensim.models.poincare.PoincareModel`
    Poincare model - embeddings for graphs.

"""

from fieldembed import utils
from fieldembed.utils import deprecated


import logging
from timeit import default_timer
import threading
from six.moves import range
from six import itervalues, string_types
from .. import matutils
from numpy import float32 as REAL, ones, random, dtype, zeros
import numpy as np
from types import GeneratorType
import warnings
import os
import copy
from datetime import datetime
try:
    from queue import Queue
except ImportError:
    from Queue import Queue

logger = logging.getLogger(__name__)


class BaseAny2VecModel(utils.SaveLoad):
    r"""Base class for training, using and evaluating \*2vec model.

    Contains implementation for multi-threaded training. The purpose of this class is to provide a
    reference interface for concrete embedding implementations, whether the input space is a corpus
    of words, documents or anything else. At the same time, functionality that we expect to be common
    for those implementations is provided here to avoid code duplication.

    In the special but usual case where the input space consists of words, a more specialized layer
    is provided, consider inheriting from :class:`~gensim.models.base_any2vec.BaseWordEmbeddingsModel`

    Notes
    -----
    A subclass should initialize the following attributes:

    * self.kv - keyed vectors in model (see :class:`~gensim.models.keyedvectors.Word2VecKeyedVectors` as example)
    * self.vocabulary - vocabulary (see :class:`~gensim.models.word2vec.Word2VecVocab` as example)
    * self.trainables - internal matrices (see :class:`~gensim.models.word2vec.Word2VecTrainables` as example)

    """
    def __init__(self, workers=3, vector_size=100, epochs=5, callbacks=(), batch_words=10000):
        """

        Parameters
        ----------
        workers : int, optional
            Number of working threads, used for multithreading.
        vector_size : int, optional
            Dimensionality of the feature vectors.
        epochs : int, optional
            Number of iterations (epochs) of training through the corpus.
        callbacks : list of :class:`~gensim.models.callbacks.CallbackAny2Vec`, optional
            List of callbacks that need to be executed/run at specific stages during training.
        batch_words : int, optional
            Number of words to be processed by a single job.

        """
        self.vector_size = int(vector_size)
        self.workers = int(workers)
        self.epochs = epochs
        self.train_count = 0
        self.total_train_time = 0
        self.batch_words = batch_words
        self.model_trimmed_post_training = False
        self.callbacks = callbacks

    def _get_job_params(self, cur_epoch):
        """Get job parameters required for each batch."""
        raise NotImplementedError()

    def _set_train_params(self, **kwargs):
        """Set model parameters required for training."""
        raise NotImplementedError()

    def _update_job_params(self, job_params, epoch_progress, cur_epoch):
        """Get updated job parameters based on the epoch_progress and cur_epoch."""
        raise NotImplementedError()

    def _get_thread_working_mem(self):
        """Get private working memory per thread."""
        raise NotImplementedError()

    def _raw_word_count(self, job):
        """Get the number of words in a given job."""
        raise NotImplementedError()

    def _clear_post_train(self):
        """Resets certain properties of the model post training. eg. `keyedvectors.vectors_norm`."""
        raise NotImplementedError()


    def _check_training_sanity(self, epochs=None, total_examples=None, total_words=None, **kwargs):
        """Check that the training parameters provided make sense. e.g. raise error if `epochs` not provided."""
        raise NotImplementedError()

    def _check_input_data_sanity(self, data_iterable=None, corpus_file=None, nlptext = None):
        """Check that only one argument is None."""
        if nlptext is None:
            if not (data_iterable is None) ^ (corpus_file is None):
                raise ValueError("You must provide only one of singlestream or corpus_file or nlptext arguments.")

    ###################################################################
    ###################################################################
    def _do_train_job(self, data_iterable, job_parameters, thread_private_mem):
        """Train a single batch. Return 2-tuple `(effective word count, total word count)`."""
        raise NotImplementedError()

    def _worker_loop(self, job_queue, progress_queue):
        thread_private_mem = self._get_thread_working_mem() # TODO: this is space to store gradients, change this.
        # eturn work, neu1
        jobs_processed = 0
        while True:
            job = job_queue.get()
            if job is None:
                progress_queue.put(None)
                break  # no more jobs => quit this worker
            data_iterable, job_parameters = job 
            # data_iterable: [[string1, string2, ...],[.], [.], ...]
            # job_parameters: current learning rate: cur_alpha

            for callback in self.callbacks:
                callback.on_batch_begin(self)

            tally, raw_tally = self._do_train_job(data_iterable, job_parameters, thread_private_mem)

            for callback in self.callbacks:
                callback.on_batch_end(self)

            progress_queue.put((len(data_iterable), tally, raw_tally))  # report back progress
            jobs_processed += 1
        logger.debug("worker exiting, processed %i jobs", jobs_processed)
    
    def _job_producer(self, data_iterator, job_queue, cur_epoch=0, total_examples=None, total_words=None):
        # data_iterator: whole data
        job_batch, batch_size = [], 0
        pushed_words, pushed_examples = 0, 0 # examples refers to sentences
        next_job_params = self._get_job_params(cur_epoch) # current learning rate: cur_alpha
        job_no = 0 # job_num

        for data_idx, data in enumerate(data_iterator):
            # here a data is a sentence
            data_length = self._raw_word_count([data])

            # can we fit this sentence into the existing job batch?
            if batch_size + data_length <= self.batch_words:
                # yes => add it to the current job
                job_batch.append(data)
                batch_size += data_length
            else:
                job_no += 1
                job_queue.put((job_batch, next_job_params)) # job_batch: [[string1, string2, ...],[.], [.], ...]

                # update the learning rate for the next job
                if total_examples:
                    # examples-based decay
                    pushed_examples += len(job_batch)
                    epoch_progress = 1.0 * pushed_examples / total_examples
                
                next_job_params = self._update_job_params(next_job_params, epoch_progress, cur_epoch)

                # add the sentence that didn't fit as the first item of a new job
                job_batch, batch_size = [data], data_length
                # next loop, next job parameters, end the whole dataset is over.
        
        # add the last job too (may be significantly smaller than batch_words)
        if job_batch:
            job_no += 1
            job_queue.put((job_batch, next_job_params))

        if job_no == 0 and self.train_count == 0:
            logger.warning(
                "train() called with an empty iterator (if not intended, "
                "be sure to provide a corpus that offers restartable iteration = an iterable)."
            )

        # give the workers heads up that they can finish -- no more work!
        for _ in range(self.workers):
            job_queue.put(None) # at the end, give 4 None s if there are 4 calculation workers.
        logger.debug("----> Worker: Job Producer loop exiting, total %i jobs", job_no)
    ################################################################### 


    ###################################################################
    ###################################################################
    def _do_train_job_nlptext(self, indexes, sentence_idx, job_parameters, thread_private_mem):
        """Train a single batch. Return 2-tuple `(effective word count, total word count)`."""
        raise NotImplementedError()

    def _worker_loop_nlptext(self, job_queue, progress_queue):
        thread_private_mem = self._get_thread_working_mem() # TODO: this is space to store gradients, change this.
        # eturn work, neu1
        jobs_processed = 0
        while True:
            job = job_queue.get()
            if job is None:
                progress_queue.put(None)
                break  # no more jobs => quit this worker
            indexes, sentence_idx, job_parameters = job 

            for callback in self.callbacks:
                callback.on_batch_begin(self)

            tally, raw_tally = self._do_train_job_nlptext(indexes, sentence_idx, job_parameters, thread_private_mem)

            for callback in self.callbacks:
                callback.on_batch_end(self)

            progress_queue.put((len(sentence_idx), tally, raw_tally))  # report back progress
            jobs_processed += 1
        logger.debug("o----> Worker exiting, processed %i jobs", jobs_processed)
    
    def _job_producer_nlptext(self, 
        sentences_endidx, total_examples, 
        tokens_vocidx, total_words, 
        batch_end_st_idx_list, job_no, job_queue,
        cur_epoch=0):
        

        #---------------------------------------------------# 
        job_batch, batch_size = [], 0
        pushed_words, pushed_examples = 0, 0 # examples refers to sentences
        next_job_params = self._get_job_params(cur_epoch) # current learning rate: cur_alpha
    
        for idx in range(job_no):

            # start and end are batch's start sentence loc_id and end sentence loc_id
            # as python routines, batch is [start, end), left close right open
            start = batch_end_st_idx_list[idx-1] if idx > 0 else 0
            end   = batch_end_st_idx_list[idx]

            # print(start, end)
            # find the start sentence's start token loc_id, and
            # find the end sentence's start token loc_id. (as the end sentence is exluded)
            token_start = sentences_endidx[start-1] if start > 0 else 0
            token_end   = sentences_endidx[end  -1]

            indexes     = tokens_vocidx[token_start:token_end] # dtype = np.uint32
            # sentence_idx= np.array([i-token_start for i in sentences_endidx[start: end]], dtype = np.uint32)
            sentence_idx= [i-token_start for i in sentences_endidx[start: end]]
            # print('The start and end sent loc_id:', start, end)
            # print('The token start and end loc idx in each batch:', token_start, token_end)
            # print(sentence_idx[-1], len(indexes), '\n')
            
            # assaure that the input is correct
            # TODO
            # print_sentence()
            job_queue.put((indexes, sentence_idx, next_job_params))

            pushed_examples += len(sentence_idx)
            epoch_progress = 1.0 * pushed_examples / total_examples

            # prepare learning rate for next job
            next_job_params = self._update_job_params(next_job_params, epoch_progress, cur_epoch)

        # print(end == len(sentences_endidx))
        # print(token_end == len(tokens_vocidx))
    
        if job_no == 0 and self.train_count == 0:
            logger.warning(
                "train() called with an empty iterator (if not intended, "
                "be sure to provide a corpus that offers restartable iteration = an iterable)."
            )

        # give the workers heads up that they can finish -- no more work!
        for _ in range(self.workers):
            job_queue.put(None) # at the end, give 4 None s if there are 4 calculation workers.
        logger.debug("----> Worker: Job Producer loop exiting, total %i jobs", job_no)
    ################################################################### 




    def _log_progress(self, job_queue, progress_queue, cur_epoch, example_count, total_examples,
                      raw_word_count, total_words, trained_word_count, elapsed):
        raise NotImplementedError()

    def _log_epoch_end(self, cur_epoch, example_count, total_examples, raw_word_count, total_words,
                       trained_word_count, elapsed, is_corpus_file_mode):
        raise NotImplementedError()

    def _log_train_end(self, raw_word_count, trained_word_count, total_elapsed, job_tally):
        raise NotImplementedError()

    ###################################################################
    def _log_epoch_progress(self, progress_queue=None, job_queue=None, cur_epoch=0, total_examples=None,total_words=None, report_delay=1.0, is_corpus_file_mode=None):

        example_count, trained_word_count, raw_word_count = 0, 0, 0
        start, next_report = default_timer() - 0.00001, 1.0
        job_tally = 0
        unfinished_worker_count = self.workers

        while unfinished_worker_count > 0:
            report = progress_queue.get()  # blocks if workers too slow
            if report is None:  # a thread reporting that it finished
                unfinished_worker_count -= 1
                logger.info("Worker thread finished; awaiting finish of %i more threads", unfinished_worker_count)
                continue
            examples, trained_words, raw_words = report
            job_tally += 1

            # update progress stats
            example_count += examples
            trained_word_count += trained_words  # only words in vocab & sampled
            raw_word_count += raw_words

            # log progress once every report_delay seconds
            elapsed = default_timer() - start
            if elapsed >= next_report:
                self._log_progress(
                    job_queue, progress_queue, cur_epoch, example_count, total_examples,
                    raw_word_count, total_words, trained_word_count, elapsed)
                next_report = elapsed + report_delay
        # all done; report the final stats
        elapsed = default_timer() - start
        self._log_epoch_end(
            cur_epoch, example_count, total_examples, raw_word_count, total_words,
            trained_word_count, elapsed, is_corpus_file_mode)
        self.total_train_time += elapsed
        return trained_word_count, raw_word_count, job_tally
    ###################################################################
    ###################################################################

    def _train_epoch(self, data_iterable, cur_epoch=0, total_examples=None, total_words=None,queue_factor=2, report_delay=1.0):
        """Train the model for a single epoch.

        Parameters
        ----------
        data_iterable : iterable of list of object
            The input corpus. This will be split in chunks and these chunks will be pushed to the queue.
        cur_epoch : int, optional
            The current training epoch, needed to compute the training parameters for each job.
            For example in many implementations the learning rate would be dropping with the number of epochs.
        total_examples : int, optional
            Count of objects in the `data_iterator`. In the usual case this would correspond to the number of sentences
            in a corpus, used to log progress.
        total_words : int, optional
            Count of total objects in `data_iterator`. In the usual case this would correspond to the number of raw
            words in a corpus, used to log progress.
        queue_factor : int, optional
            Multiplier for size of queue -> size = number of workers * queue_factor.
        report_delay : float, optional
            Number of seconds between two consecutive progress report messages in the logger.

        Returns
        -------
        (int, int, int)
            The training report for this epoch consisting of three elements:
                * Size of data chunk processed, for example number of sentences in the corpus chunk.
                * Effective word count used in training (after ignoring unknown words and trimming the sentence length).
                * Total word count used in training.

        """
        job_queue = Queue(maxsize=queue_factor * self.workers)
        progress_queue = Queue(maxsize=(queue_factor + 1) * self.workers)

        workers = [
            threading.Thread(
                target=self._worker_loop,
                args=(job_queue, progress_queue,))
            for _ in range(self.workers)
        ]

        workers.append(threading.Thread(
            target=self._job_producer,
            args=(data_iterable, job_queue,), # data_iterable is sentences
            kwargs={'cur_epoch': cur_epoch, 'total_examples': total_examples, 'total_words': total_words}))

        for thread in workers:
            thread.daemon = True  # make interrupting the process with ctrl+c easier
            thread.start()

        trained_word_count, raw_word_count, job_tally = self._log_epoch_progress(
            progress_queue, job_queue, cur_epoch=cur_epoch, total_examples=total_examples, total_words=total_words,
            report_delay=report_delay, is_corpus_file_mode=False)

        return trained_word_count, raw_word_count, job_tally

    def _train_epoch_nlptext(self, nlptext, cur_epoch=0, total_examples=None, total_words=None,queue_factor=2, report_delay=1.0):
        
        ########### preprocess
        sentences_endidx = nlptext.SENT['EndIDXTokens']
        tokens_vocidx    = nlptext.TOKEN['ORIGTokenIndex']
        total_examples  =  len(sentences_endidx)
        total_words =  len(tokens_vocidx)           

        ####################################### get batch_end_st_idx_list and job_no
        print('Start getting batch infos')
        s = datetime.now(); print(s)
        batch_end_st_idx_list, job_no = nlptext.Calculate_Infos(self.batch_words)
        e = datetime.now(); print(e)
        print('The time of finding batch_end_st_idx_list:', e - s)
        print('Total job number is:', job_no)
        ####################################### get batch_end_st_idx_list and job_no

        # sentences_endidx, tokens_vocidx, batch_end_st_idx_list, job_no, 
        job_queue = Queue(maxsize=queue_factor * self.workers)
        progress_queue = Queue(maxsize=(queue_factor + 1) * self.workers)


        workers = [
            threading.Thread(
                target=self._worker_loop_nlptext,
                args=(job_queue, progress_queue,))
            for _ in range(self.workers)
        ]
        logger.info('\n the total_examples is:' + str(total_examples) + '   , the total words is:' + str(total_words) + '\n')
        workers.append(threading.Thread(
            target=self._job_producer_nlptext,
            args=(sentences_endidx, total_examples, tokens_vocidx, total_words, batch_end_st_idx_list, job_no, job_queue,), # data_iterable is sentences
            kwargs={'cur_epoch': cur_epoch,}))

        for thread in workers:
            thread.daemon = True  # make interrupting the process with ctrl+c easier
            thread.start()

        trained_word_count, raw_word_count, job_tally = self._log_epoch_progress(
            progress_queue, job_queue, cur_epoch=cur_epoch, total_examples=total_examples, total_words=total_words,
            report_delay=report_delay, is_corpus_file_mode=False)

        return trained_word_count, raw_word_count, job_tally

    def train(self, data_iterable=None, corpus_file=None, nlptext = None, epochs=None, total_examples=None, total_words=None, queue_factor=2, report_delay=1.0, callbacks=(), **kwargs):
    
        self._set_train_params(**kwargs)
        if callbacks:
            self.callbacks = callbacks
        self.epochs = epochs
        self._check_training_sanity(
            epochs=epochs,
            total_examples=total_examples,
            total_words=total_words, **kwargs)

        for callback in self.callbacks:
            callback.on_train_begin(self)

        # print('in train')
        # print(total_examples)
        # print(total_words)
        trained_word_count = 0
        raw_word_count = 0
        start = default_timer() - 0.00001
        job_tally = 0

        for cur_epoch in range(self.epochs):
            for callback in self.callbacks:
                callback.on_epoch_begin(self)

            ###########################################################
            # ONE EPOCH
            # data_iterable -> sentences
            if data_iterable is not None: # here is sentences
                # total_examples: sentence_num
                # total_words:    words_num
                # queue_factor: 2
                # report_delay: 1.0
                trained_word_count_epoch, raw_word_count_epoch, job_tally_epoch = self._train_epoch(
                    data_iterable, cur_epoch=cur_epoch, total_examples=total_examples,
                    total_words=total_words, queue_factor=queue_factor, report_delay=report_delay
                )

            elif nlptext is not None:

                trained_word_count_epoch, raw_word_count_epoch, job_tally_epoch = self._train_epoch_nlptext(
                    nlptext, cur_epoch=cur_epoch, total_examples=total_examples,
                    total_words=total_words, queue_factor=queue_factor, report_delay=report_delay
                )

            else:
                print('No Training Data is Provided...')

            trained_word_count += trained_word_count_epoch
            raw_word_count += raw_word_count_epoch
            job_tally += job_tally_epoch

            for callback in self.callbacks:
                callback.on_epoch_end(self)

        # Log overall time
        total_elapsed = default_timer() - start
        self._log_train_end(raw_word_count, trained_word_count, total_elapsed, job_tally)

        self.train_count += 1  # number of times train() has been called
        self._clear_post_train()

        for callback in self.callbacks:
            callback.on_train_end(self)
        return trained_word_count, raw_word_count

    @classmethod
    def load(cls, fname_or_handle, **kwargs):
        """Load a previously saved object (using :meth:`gensim.models.base_any2vec.BaseAny2VecModel.save`) from a file.

        Parameters
        ----------
        fname_or_handle : {str, file-like object}
            Path to file that contains needed object or handle to an open file.
        **kwargs : object
            Keyword arguments propagated to :meth:`~gensim.utils.SaveLoad.load`.

        See Also
        --------
        :meth:`~gensim.models.base_any2vec.BaseAny2VecModel.save`
            Method for save a model.

        Returns
        -------
        object
            Object loaded from `fname_or_handle`.

        Raises
        ------
        IOError
            When methods are called on an instance (should be called on a class, this is a class method).

        """
        return super(BaseAny2VecModel, cls).load(fname_or_handle, **kwargs)

    def save(self, fname_or_handle, **kwargs):
        """"Save the object to file.

        Parameters
        ----------
        fname_or_handle : {str, file-like object}
            Path to file where the model will be persisted.
        **kwargs : object
            Key word arguments propagated to :meth:`~gensim.utils.SaveLoad.save`.

        See Also
        --------
        :meth:`~gensim.models.base_any2vec.BaseAny2VecModel.load`
            Method for load model after current method.

        """
        super(BaseAny2VecModel, self).save(fname_or_handle, **kwargs)

class BaseWordEmbeddingsModel(BaseAny2VecModel):
    """Base class containing common methods for training, using & evaluating word embeddings learning models.

    See Also
    --------
    :class:`~gensim.models.word2vec.Word2Vec`.
        Word2Vec model - embeddings for words.
    :class:`~gensim.models.fasttext.FastText`.
        FastText model - embeddings for words (ngram-based).
    :class:`~gensim.models.doc2vec.Doc2Vec`.
        Doc2Vec model - embeddings for documents.
    :class:`~gensim.models.poincare.PoincareModel`
        Poincare model - embeddings for graphs.

    """
    def _clear_post_train(self):
        raise NotImplementedError()

    def _do_train_job(self, data_iterable, job_parameters, thread_private_mem):
        raise NotImplementedError()

    def _set_train_params(self, **kwargs):
        raise NotImplementedError()

    def __init__(self, sentences=None, corpus_file=None, nlptext = None,  workers=3, vector_size=100, epochs=5, callbacks=(),
                 batch_words=10000, trim_rule=None, sg=0, alpha=0.025, window=5, seed=1, hs=0, negative=5,
                 ns_exponent=0.75, cbow_mean=1, min_alpha=0.0001, compute_loss=False, fast_version=0, **kwargs):
        
        if vector_size % 4 != 0:
            logger.warning("consider setting layer size to a multiple of 4 for greater performance")
        
        self.window = int(window)
        self.random = random.RandomState(seed) # random = seed
        
        self.sg = int(sg)

        self.hs = int(hs) # should be removed
        self.negative = int(negative)

        self.ns_exponent = ns_exponent
        self.cbow_mean = int(cbow_mean)
        self.compute_loss = bool(compute_loss)
        self.running_training_loss = 0
 
        self.alpha = float(alpha)
        self.min_alpha_yet_reached = float(alpha)
        self.min_alpha = float(min_alpha)

        self.corpus_count = 0
        self.corpus_total_words = 0

        super(BaseWordEmbeddingsModel, self).__init__(
            workers=workers, vector_size=vector_size, epochs=epochs, callbacks=callbacks, batch_words=batch_words)

        if fast_version < 0:
            # other wise, it don't need to create self.neg_labels
            warnings.warn(
                "C extension not loaded, training will be slow. "
                "Install a C compiler and reinstall gensim for fast training."
            )
            self.neg_labels = []
            if self.negative > 0:
                # precompute negative labels optimization for pure-python training
                self.neg_labels = zeros(self.negative + 1)
                self.neg_labels[0] = 1.

        if sentences is not None or corpus_file is not None or nlptext is not None:
            # If we have the data to train.
            self._check_input_data_sanity(data_iterable=sentences, corpus_file=corpus_file, nlptext = nlptext)
            
            if corpus_file is not None and not isinstance(corpus_file, string_types):
                raise TypeError("You must pass string as the corpus_file argument.")
            elif isinstance(sentences, GeneratorType):
                raise TypeError("You can't pass a generator as the sentences argument. Try a sequence.")


            # first build_vocab
            self.build_vocab(sentences=sentences, corpus_file=corpus_file, nlptext = nlptext, trim_rule=trim_rule)
            
            # then train
            print('\n\n======== Training Start ....'); s = datetime.now()
            self.train(
                sentences=sentences, corpus_file = corpus_file, nlptext = nlptext, total_examples=self.corpus_count,
                total_words=self.corpus_total_words, epochs=self.epochs, start_alpha=self.alpha,
                end_alpha=self.min_alpha, compute_loss=compute_loss)
            print('======== Training End ......'); e = datetime.now()
            print('======== Total Time: ', e - s)

        else:
            if trim_rule is not None:
                logger.warning(
                    "The rule, if given, is only used to prune vocabulary during build_vocab() "
                    "and is not stored as part of the model. Model initialized without sentences. "
                    "trim_rule provided, if any, will be ignored.")

    # this seems to be important.
    def build_vocab(self, sentences=None, corpus_file=None, nlptext = None, update=False, progress_per=10000, keep_raw_vocab=False, trim_rule=None, **kwargs):
        
        ## my code:
        if nlptext is not None:
            # scan_vocab and prepare_vocab
            # build .wv.vocab + .wv.index2word + .wv.cum_table

            print('======== Build_vocab based on NLPText....'); s = datetime.now()

            print('-------> Prepare Vocab....')
            total_words, corpus_count,  report_values = self.vocabulary.scan_and_prepare_vocab_from_nlptext(nlptext, 
                                                                        self.negative, self.wv, update=update, **kwargs) 

            self.corpus_count = corpus_count
            self.corpus_total_words = total_words
            
            report_values['memory'] = self.estimate_memory(vocab_size=report_values['num_retained_words'])

            print('-------> Prepare Trainable Weight....')
            self.trainables.prepare_weights_from_nlptext(self.negative, self.wv, update=update, vocabulary=self.vocabulary)

            print('======== The Voc and Parameters are Ready!'); e = datetime.now()
            print('======== Total Time: ', e - s)

        else:
            #########################################################################
            # .vocabulary.scan_vocab and .vocabulary.prepare_vocab produces:
            # .wv.vocab (DTU, key:string, value(index, count, prob)) + .wv.index2word + .wv.cum_table and other attributes
            print('======== Build_vocab based on LineSentence....'); s = datetime.now()
            total_words, corpus_count = self.vocabulary.scan_vocab(sentences=sentences, corpus_file=corpus_file, 
                                                                   progress_per=progress_per, trim_rule=trim_rule)
            self.corpus_count = corpus_count
            self.corpus_total_words = total_words
            
            report_values = self.vocabulary.prepare_vocab(self.hs, self.negative, self.wv, 
                                                          update=update, keep_raw_vocab=keep_raw_vocab,
                                                          trim_rule=trim_rule, **kwargs)
            #########################################################################

            report_values['memory'] = self.estimate_memory(vocab_size=report_values['num_retained_words'])
            
            print('-------> Prepare Trainable Weight....')
            self.trainables.prepare_weights(self.hs, self.negative, self.wv, update=update, vocabulary=self.vocabulary)

            print('======== The Voc and Parameters are Ready!'); e = datetime.now()
            print('======== Total Time: ', e - s)

    def build_vocab_from_freq(self, word_freq, keep_raw_vocab=False, corpus_count=None, trim_rule=None, update=False):
        """Build vocabulary from a dictionary of word frequencies.

        Parameters
        ----------
        word_freq : dict of (str, int)
            A mapping from a word in the vocabulary to its frequency count.
        keep_raw_vocab : bool, optional
            If False, delete the raw vocabulary after the scaling is done to free up RAM.
        corpus_count : int, optional
            Even if no corpus is provided, this argument can set corpus_count explicitly.
        trim_rule : function, optional
            Vocabulary trimming rule, specifies whether certain words should remain in the vocabulary,
            be trimmed away, or handled using the default (discard if word count < min_count).
            Can be None (min_count will be used, look to :func:`~gensim.utils.keep_vocab_item`),
            or a callable that accepts parameters (word, count, min_count) and returns either
            :attr:`gensim.utils.RULE_DISCARD`, :attr:`gensim.utils.RULE_KEEP` or :attr:`gensim.utils.RULE_DEFAULT`.
            The rule, if given, is only used to prune vocabulary during current method call and is not stored as part
            of the model.

            The input parameters are of the following types:
                * `word` (str) - the word we are examining
                * `count` (int) - the word's frequency count in the corpus
                * `min_count` (int) - the minimum count threshold.

        update : bool, optional
            If true, the new provided words in `word_freq` dict will be added to model's vocab.

        """
        logger.info("Processing provided word frequencies")
        # Instead of scanning text, this will assign provided word frequencies dictionary(word_freq)
        # to be directly the raw vocab
        raw_vocab = word_freq
        logger.info(
            "collected %i different raw word, with total frequency of %i",
            len(raw_vocab), sum(itervalues(raw_vocab))
        )

        # Since no sentences are provided, this is to control the corpus_count.
        self.corpus_count = corpus_count or 0
        self.vocabulary.raw_vocab = raw_vocab

        # trim by min_count & precalculate downsampling
        report_values = self.vocabulary.prepare_vocab(
            self.hs, self.negative, self.wv, keep_raw_vocab=keep_raw_vocab,
            trim_rule=trim_rule, update=update)
        report_values['memory'] = self.estimate_memory(vocab_size=report_values['num_retained_words'])
        self.trainables.prepare_weights(
            self.hs, self.negative, self.wv, update=update, vocabulary=self.vocabulary)  # build tables & arrays

    def estimate_memory(self, vocab_size=None, report=None):
        """Estimate required memory for a model using current settings and provided vocabulary size.

        Parameters
        ----------
        vocab_size : int, optional
            Number of unique tokens in the vocabulary
        report : dict of (str, int), optional
            A dictionary from string representations of the model's memory consuming members to their size in bytes.

        Returns
        -------
        dict of (str, int)
            A dictionary from string representations of the model's memory consuming members to their size in bytes.

        """
        vocab_size = vocab_size or len(self.wv.vocab)
        report = report or {}
        report['vocab'] = vocab_size * (700 if self.hs else 500)
        report['vectors'] = vocab_size * self.vector_size * dtype(REAL).itemsize
        # if self.hs:
        #     report['syn1'] = vocab_size * self.trainables.layer1_size * dtype(REAL).itemsize
        if self.negative:
            report['syn1neg'] = vocab_size * self.trainables.layer1_size * dtype(REAL).itemsize
        report['total'] = sum(report.values())
        logger.info(
            "estimated required memory for %i words and %i dimensions: %i bytes",
            vocab_size, self.vector_size, report['total']
        )
        return report

    def train(self, sentences=None, corpus_file=None, nlptext = None, total_examples=None, total_words=None,
              epochs=None, start_alpha=None, end_alpha=None, word_count=0,
              queue_factor=2, report_delay=1.0, compute_loss=False, callbacks=(), **kwargs):

        self.alpha = start_alpha or self.alpha
        self.min_alpha = end_alpha or self.min_alpha
        self.compute_loss = compute_loss
        self.running_training_loss = 0.0
        return super(BaseWordEmbeddingsModel, self).train(
            data_iterable=sentences, corpus_file=corpus_file, nlptext = nlptext, total_examples=total_examples,
            total_words=total_words, epochs=epochs, start_alpha=start_alpha, end_alpha=end_alpha, word_count=word_count,
            queue_factor=queue_factor, report_delay=report_delay, compute_loss=compute_loss, callbacks=callbacks,
            **kwargs)


    def _get_thread_working_mem(self):
        work = matutils.zeros_aligned(self.trainables.layer1_size, dtype=REAL)  # per-thread private work memory
        neu1 = matutils.zeros_aligned(self.trainables.layer1_size, dtype=REAL)
        return work, neu1

    def _get_job_params(self, cur_epoch):
        """Get the learning rate used in the current epoch.

        Parameters
        ----------
        cur_epoch : int
            Current iteration through the corpus

        Returns
        -------
        float
            The learning rate for this epoch (it is linearly reduced with epochs from `self.alpha` to `self.min_alpha`).

        """
        alpha = self.alpha - ((self.alpha - self.min_alpha) * float(cur_epoch) / self.epochs)
        return alpha

    def _update_job_params(self, job_params, epoch_progress, cur_epoch):
        start_alpha = self.alpha
        end_alpha = self.min_alpha
        progress = (cur_epoch + epoch_progress) / self.epochs
        next_alpha = start_alpha - (start_alpha - end_alpha) * progress
        next_alpha = max(end_alpha, next_alpha)
        self.min_alpha_yet_reached = next_alpha
        return next_alpha

    def _raw_word_count(self, job):
        """Get the number of words in a given job.

        Parameters
        ----------
        job: iterable of list of str
            The corpus chunk processed in a single batch.

        Returns
        -------
        int
            Number of raw words in the corpus chunk.

        """
        return sum(len(sentence) for sentence in job)

    def _check_training_sanity(self, epochs=None, total_examples=None, total_words=None, **kwargs):
        """Checks whether the training parameters make sense.

        Called right before training starts in :meth:`~gensim.models.base_any2vec.BaseWordEmbeddingsModel.train`
        and raises warning or errors depending on the severity of the issue in case an inconsistent parameter
        combination is detected.

        Parameters
        ----------
        epochs : int, optional
            Number of training epochs. Must have a (non None) value.
        total_examples : int, optional
            Number of documents in the corpus. Either `total_examples` or `total_words` **must** be supplied.
        total_words : int, optional
            Number of words in the corpus. Either `total_examples` or `total_words` **must** be supplied.
        **kwargs : object
            Unused. Present to preserve signature among base and inherited implementations.

        Raises
        ------
        RuntimeError
            If one of the required training pre/post processing steps have not been performed.
        ValueError
            If the combination of input parameters is inconsistent.

        """
        if self.alpha > self.min_alpha_yet_reached:
            logger.warning("Effective 'alpha' higher than previous training cycles")
        if self.model_trimmed_post_training:
            raise RuntimeError("Parameters for training were discarded using model_trimmed_post_training method")

        if not self.wv.vocab:  # should be set by `build_vocab`
            raise RuntimeError("you must first build vocabulary before training the model")
        if not len(self.wv.vectors):
            raise RuntimeError("you must initialize vectors before training the model")

        if not hasattr(self, 'corpus_count'):
            raise ValueError(
                "The number of examples in the training corpus is missing. "
                "Please make sure this is set inside `build_vocab` function."
                "Call the `build_vocab` function before calling `train`."
            )

        if total_words is None and total_examples is None:
            raise ValueError(
                "You must specify either total_examples or total_words, for proper job parameters updation"
                "and progress calculations. "
                "The usual value is total_examples=model.corpus_count."
            )
        if epochs is None:
            raise ValueError("You must specify an explict epochs count. The usual value is epochs=model.epochs.")
        logger.info(
            "training model with %i workers on %i vocabulary and %i features, "
            "using sg=%s hs=%s sample=%s negative=%s window=%s",
            self.workers, len(self.wv.vocab), self.trainables.layer1_size, self.sg,
            self.hs, self.vocabulary.sample, self.negative, self.window
        )

    @classmethod
    def load(cls, *args, **kwargs):
        """Load a previously saved object (using :meth:`~gensim.models.base_any2vec.BaseWordEmbeddingsModel.save`) from file.

        Also initializes extra instance attributes in case the loaded model does not include them.
        `*args` or `**kwargs` **MUST** include the fname argument (path to saved file).
        See :meth:`~gensim.utils.SaveLoad.load`.

        Parameters
        ----------
        *args : object
            Positional arguments passed to :meth:`~gensim.utils.SaveLoad.load`.
        **kwargs : object
            Key word arguments passed to :meth:`~gensim.utils.SaveLoad.load`.

        See Also
        --------
        :meth:`~gensim.models.base_any2vec.BaseWordEmbeddingsModel.save`
            Method for save a model.

        Returns
        -------
        :class:`~gensim.models.base_any2vec.BaseWordEmbeddingsModel`
            Model loaded from disk.

        Raises
        ------
        IOError
            When methods are called on instance (should be called from class).

        """
        model = super(BaseWordEmbeddingsModel, cls).load(*args, **kwargs)
        if not hasattr(model, 'ns_exponent'):
            model.ns_exponent = 0.75
        if not hasattr(model.vocabulary, 'ns_exponent'):
            model.vocabulary.ns_exponent = 0.75
        if model.negative and hasattr(model.wv, 'index2word'):
            model.vocabulary.make_cum_table(model.wv)  # rebuild cum_table from vocabulary
        if not hasattr(model, 'corpus_count'):
            model.corpus_count = None
        if not hasattr(model, 'corpus_total_words'):
            model.corpus_total_words = None
        if not hasattr(model.trainables, 'vectors_lockf') and hasattr(model.wv, 'vectors'):
            model.trainables.vectors_lockf = ones(len(model.wv.vectors), dtype=REAL)
        if not hasattr(model, 'random'):
            model.random = random.RandomState(model.trainables.seed)
        if not hasattr(model, 'train_count'):
            model.train_count = 0
            model.total_train_time = 0
        return model

    def _log_progress(self, job_queue, progress_queue, cur_epoch, example_count, total_examples,
                      raw_word_count, total_words, trained_word_count, elapsed):
        if total_examples:
            # examples-based progress %
            logger.info(
                "EPOCH %i - PROGRESS: at %.2f%% examples, %.0f words/s, in_qsize %i, out_qsize %i",
                cur_epoch + 1, 
                100.0 * example_count / total_examples, 
                trained_word_count / elapsed,
                -1 if job_queue is None else utils.qsize(job_queue), 
                utils.qsize(progress_queue)
            )
        else:
            # words-based progress %
            logger.info(
                "EPOCH %i - PROGRESS: at %.2f%% words, %.0f words/s, in_qsize %i, out_qsize %i",
                cur_epoch + 1, 
                100.0 * raw_word_count / total_words, 
                trained_word_count / elapsed,
                -1 if job_queue is None else utils.qsize(job_queue), 
                utils.qsize(progress_queue)
            )

    def _log_epoch_end(self, cur_epoch, example_count, total_examples, raw_word_count, total_words,
                       trained_word_count, elapsed, is_corpus_file_mode):
        logger.info(
            "EPOCH - %i : training on %i raw words (%i effective words) took %.1fs, %.0f effective words/s",
            cur_epoch + 1, raw_word_count, trained_word_count, elapsed, trained_word_count / elapsed
        )

        # don't warn if training in file-based mode, because it's expected behavior
        if is_corpus_file_mode:
            return

        # check that the input corpus hasn't changed during iteration
        if total_examples and total_examples != example_count:
            logger.warning(
                "EPOCH - %i : supplied example count (%i) did not equal expected count (%i)", cur_epoch + 1,
                example_count, total_examples
            )
        if total_words and total_words != raw_word_count:
            logger.warning(
                "EPOCH - %i : supplied raw word count (%i) did not equal expected count (%i)", cur_epoch + 1,
                raw_word_count, total_words
            )

    def _log_train_end(self, raw_word_count, trained_word_count, total_elapsed, job_tally):

        logger.info(
            "training on a %i raw words (%i effective words) took %.1fs, %.0f effective words/s",
            raw_word_count, trained_word_count, total_elapsed, trained_word_count / total_elapsed
        )
        if job_tally < 10 * self.workers:
            logger.warning(
                "under 10 jobs per worker: consider setting a smaller `batch_words' for smoother alpha decay"
            )

    # for backward compatibility
    @deprecated("Method will be removed in 4.0.0, use self.wv.most_similar() instead")
    def most_similar(self, positive=None, negative=None, topn=10, restrict_vocab=None, indexer=None):

        return self.wv.most_similar(positive, negative, topn, restrict_vocab, indexer)

    @deprecated("Method will be removed in 4.0.0, use self.wv.wmdistance() instead")
    def wmdistance(self, document1, document2):
        """Deprecated, use self.wv.wmdistance() instead.

        Refer to the documentation for :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.wmdistance`.

        """
        return self.wv.wmdistance(document1, document2)

    @deprecated("Method will be removed in 4.0.0, use self.wv.most_similar_cosmul() instead")
    def most_similar_cosmul(self, positive=None, negative=None, topn=10):
        """Deprecated, use self.wv.most_similar_cosmul() instead.

        Refer to the documentation for
        :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.most_similar_cosmul`.

        """
        return self.wv.most_similar_cosmul(positive, negative, topn)

    @deprecated("Method will be removed in 4.0.0, use self.wv.similar_by_word() instead")
    def similar_by_word(self, word, topn=10, restrict_vocab=None):
        """Deprecated, use self.wv.similar_by_word() instead.

        Refer to the documentation for :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.similar_by_word`.

        """
        return self.wv.similar_by_word(word, topn, restrict_vocab)

    @deprecated("Method will be removed in 4.0.0, use self.wv.similar_by_vector() instead")
    def similar_by_vector(self, vector, topn=10, restrict_vocab=None):
        """Deprecated, use self.wv.similar_by_vector() instead.

        Refer to the documentation for :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.similar_by_vector`.

        """
        return self.wv.similar_by_vector(vector, topn, restrict_vocab)

    @deprecated("Method will be removed in 4.0.0, use self.wv.doesnt_match() instead")
    def doesnt_match(self, words):
        """Deprecated, use self.wv.doesnt_match() instead.

        Refer to the documentation for :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.doesnt_match`.

        """
        return self.wv.doesnt_match(words)

    @deprecated("Method will be removed in 4.0.0, use self.wv.similarity() instead")
    def similarity(self, w1, w2):
        """Deprecated, use self.wv.similarity() instead.

        Refer to the documentation for :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.similarity`.

        """
        return self.wv.similarity(w1, w2)

    @deprecated("Method will be removed in 4.0.0, use self.wv.n_similarity() instead")
    def n_similarity(self, ws1, ws2):
        """Deprecated, use self.wv.n_similarity() instead.

        Refer to the documentation for :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.n_similarity`.

        """
        return self.wv.n_similarity(ws1, ws2)

    @deprecated("Method will be removed in 4.0.0, use self.wv.evaluate_word_pairs() instead")
    def evaluate_word_pairs(self, pairs, delimiter='\t', restrict_vocab=300000,
                            case_insensitive=True, dummy4unknown=False):
        """Deprecated, use self.wv.evaluate_word_pairs() instead.

        Refer to the documentation for
        :meth:`~gensim.models.keyedvectors.WordEmbeddingsKeyedVectors.evaluate_word_pairs`.

        """
        return self.wv.evaluate_word_pairs(pairs, delimiter, restrict_vocab, case_insensitive, dummy4unknown)


    # for backward compatibility (aliases pointing to corresponding variables in trainables, vocabulary)
    @property
    @deprecated("Attribute will be removed in 4.0.0, use self.epochs instead")
    def iter(self):
        return self.epochs

    @iter.setter
    @deprecated("Attribute will be removed in 4.0.0, use self.epochs instead")
    def iter(self, value):
        self.epochs = value

    @property
    @deprecated("Attribute will be removed in 4.0.0, use self.trainables.syn1 instead")
    def syn1(self):
        return self.trainables.syn1

    @syn1.setter
    @deprecated("Attribute will be removed in 4.0.0, use self.trainables.syn1 instead")
    def syn1(self, value):
        self.trainables.syn1 = value

    @syn1.deleter
    @deprecated("Attribute will be removed in 4.0.0, use self.trainables.syn1 instead")
    def syn1(self):
        del self.trainables.syn1

    @property
    @deprecated("Attribute will be removed in 4.0.0, use self.trainables.syn1neg instead")
    def syn1neg(self):
        return self.trainables.syn1neg

    @syn1neg.setter
    @deprecated("Attribute will be removed in 4.0.0, use self.trainables.syn1neg instead")
    def syn1neg(self, value):
        self.trainables.syn1neg = value

    @syn1neg.deleter
    @deprecated("Attribute will be removed in 4.0.0, use self.trainables.syn1neg instead")
    def syn1neg(self):
        del self.trainables.syn1neg

    @property
    @deprecated("Attribute will be removed in 4.0.0, use self.trainables.vectors_lockf instead")
    def syn0_lockf(self):
        return self.trainables.vectors_lockf

    @syn0_lockf.setter
    @deprecated("Attribute will be removed in 4.0.0, use self.trainables.vectors_lockf instead")
    def syn0_lockf(self, value):
        self.trainables.vectors_lockf = value

    @syn0_lockf.deleter
    @deprecated("Attribute will be removed in 4.0.0, use self.trainables.vectors_lockf instead")
    def syn0_lockf(self):
        del self.trainables.vectors_lockf

    @property
    @deprecated("Attribute will be removed in 4.0.0, use self.trainables.layer1_size instead")
    def layer1_size(self):
        return self.trainables.layer1_size

    @layer1_size.setter
    @deprecated("Attribute will be removed in 4.0.0, use self.trainables.layer1_size instead")
    def layer1_size(self, value):
        self.trainables.layer1_size = value

    @property
    @deprecated("Attribute will be removed in 4.0.0, use self.trainables.hashfxn instead")
    def hashfxn(self):
        return self.trainables.hashfxn

    @hashfxn.setter
    @deprecated("Attribute will be removed in 4.0.0, use self.trainables.hashfxn instead")
    def hashfxn(self, value):
        self.trainables.hashfxn = value

    @property
    @deprecated("Attribute will be removed in 4.0.0, use self.vocabulary.sample instead")
    def sample(self):
        return self.vocabulary.sample

    @sample.setter
    @deprecated("Attribute will be removed in 4.0.0, use self.vocabulary.sample instead")
    def sample(self, value):
        self.vocabulary.sample = value

    @property
    @deprecated("Attribute will be removed in 4.0.0, use self.vocabulary.min_count instead")
    def min_count(self):
        return self.vocabulary.min_count

    @min_count.setter
    @deprecated("Attribute will be removed in 4.0.0, use self.vocabulary.min_count instead")
    def min_count(self, value):
        self.vocabulary.min_count = value

    @property
    @deprecated("Attribute will be removed in 4.0.0, use self.vocabulary.cum_table instead")
    def cum_table(self):
        return self.vocabulary.cum_table

    @cum_table.setter
    @deprecated("Attribute will be removed in 4.0.0, use self.vocabulary.cum_table instead")
    def cum_table(self, value):
        self.vocabulary.cum_table = value

    @cum_table.deleter
    @deprecated("Attribute will be removed in 4.0.0, use self.vocabulary.cum_table instead")
    def cum_table(self):
        del self.vocabulary.cum_table

    def __str__(self):
        """Get a human readable representation of the object.

        Returns
        -------
        str
            A human readable string containing the class name, as well as the size of dictionary, number of
            features and starting learning rate used by the object.

        """
        return "%s(vocab=%s, size=%s, alpha=%s)" % (
            self.__class__.__name__, len(self.wv.index2word), self.vector_size, self.alpha
        )