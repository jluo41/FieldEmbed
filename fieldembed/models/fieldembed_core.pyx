#!/usr/bin/env cython
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
# cython: embedsignature=True
# coding: utf-8

import cython
import numpy as np

cimport numpy as np

from libc.math cimport exp
from libc.math cimport log
from libc.string cimport memset
from libcpp.map cimport map

# scipy <= 0.15
try:
    from scipy.linalg.blas import fblas
except ImportError:
    # in scipy > 0.15, fblas function has been removed
    import scipy.linalg.blas as fblas

REAL = np.float32

DEF MAX_SENTENCE_LEN = 10000

cdef scopy_ptr scopy=<scopy_ptr>PyCObject_AsVoidPtr(fblas.scopy._cpointer)  # y = x
cdef saxpy_ptr saxpy=<saxpy_ptr>PyCObject_AsVoidPtr(fblas.saxpy._cpointer)  # y += alpha * x
cdef sdot_ptr  sdot =<sdot_ptr> PyCObject_AsVoidPtr(fblas.sdot._cpointer)   # float = dot(x, y)
cdef dsdot_ptr dsdot=<dsdot_ptr>PyCObject_AsVoidPtr(fblas.sdot._cpointer)   # double = dot(x, y)
cdef snrm2_ptr snrm2=<snrm2_ptr>PyCObject_AsVoidPtr(fblas.snrm2._cpointer)  # sqrt(x^2)
cdef sscal_ptr sscal=<sscal_ptr>PyCObject_AsVoidPtr(fblas.sscal._cpointer)  # x = alpha * x

DEF EXP_TABLE_SIZE = 1000
DEF MAX_EXP = 6

cdef REAL_t[EXP_TABLE_SIZE] EXP_TABLE
cdef REAL_t[EXP_TABLE_SIZE] LOG_TABLE

cdef int ONE = 1
cdef REAL_t ONEF = <REAL_t>1.0


############################################## UTILS TOOL
# for when fblas.sdot returns a double
cdef REAL_t our_dot_double(const int *N, const float *X, const int *incX, const float *Y, const int *incY) nogil:
    return <REAL_t>dsdot(N, X, incX, Y, incY)

# for when fblas.sdot returns a float
cdef REAL_t our_dot_float(const int *N, const float *X, const int *incX, const float *Y, const int *incY) nogil:
    return <REAL_t>sdot(N, X, incX, Y, incY)

# for when no blas available
cdef REAL_t our_dot_noblas(const int *N, const float *X, const int *incX, const float *Y, const int *incY) nogil:
    # not a true full dot()-implementation: just enough for our cases
    cdef int i
    cdef REAL_t a
    a = <REAL_t>0.0
    for i from 0 <= i < N[0] by 1:
        a += X[i] * Y[i]
    return a

# for when no blas available
cdef void our_saxpy_noblas(const int *N, const float *alpha, const float *X, const int *incX, float *Y, const int *incY) nogil:
    cdef int i
    for i from 0 <= i < N[0] by 1:
        Y[i * (incY[0])] = (alpha[0]) * X[i * (incX[0])] + Y[i * (incY[0])]

# to support random draws from negative-sampling cum_table
cdef inline unsigned long long bisect_left(np.uint32_t *a, unsigned long long x, unsigned long long lo, unsigned long long hi) nogil:
    cdef unsigned long long mid
    while hi > lo:
        mid = (lo + hi) >> 1
        if a[mid] >= x:
            hi = mid
        else:
            lo = mid + 1
    return lo

cdef inline unsigned long long random_int32(unsigned long long *next_random) nogil:
    cdef unsigned long long this_random = next_random[0] >> 16
    next_random[0] = (next_random[0] * <unsigned long long>25214903917ULL + 11) & 281474976710655ULL
    return this_random


cdef int SUBSAMPLING = 1
############################################## UTILS TOOL


################################################################# Field Embedding 0X1
#--> NEW for 0X1_neat
cdef init_w2v_config_0X1_neat(
    Word2VecConfig *c, 
    model, 
    alpha, 
    compute_loss, 
    _work, 
    _neu1):
    #===========================================================================================#
    cdef int i
    cdef int fld_idx

    c[0].sg = model.sg
    c[0].negative = model.negative
    c[0].sample = (model.vocabulary.sample != 0)
    c[0].cbow_mean = model.standard_grad
    c[0].window = model.window
    c[0].workers = model.workers
    c[0].compute_loss = (1 if compute_loss else 0)
    c[0].running_training_loss = model.running_training_loss

    #######################################################################
    fld_idx = -1 
    c[0].use_sub  = model.use_sub
    if c[0].use_sub:
        fld_idx  = fld_idx + 1
        c[0].syn0_map[fld_idx]    = <REAL_t *>(np.PyArray_DATA(model.field_sub[0][0][0].vectors))
        c[0].LookUp_map[fld_idx]  = <np.uint32_t *>(np.PyArray_DATA(model.field_sub[0][0][1]))
        c[0].EndIdx_map[fld_idx]  = <np.uint32_t *>(np.PyArray_DATA(model.field_sub[0][0][2]))
        c[0].LengInv_map[fld_idx] = <REAL_t *>(np.PyArray_DATA(model.field_sub[0][0][3])) 
        c[0].leng_max_map[fld_idx]= model.field_sub[0][0][4]      

    c[0].use_head = model.use_head # token
    if c[0].use_head:
        fld_idx  = fld_idx + 1
        c[0].syn0_map[fld_idx] = <REAL_t *>(np.PyArray_DATA(model.field_head[0][1].vectors)) 
    #######################################################################

    c[0].word_locks = <REAL_t *>(np.PyArray_DATA(model.trainables.vectors_lockf))
    c[0].alpha = alpha
    c[0].size = model.wv.vector_size

    if c[0].negative:
        c[0].syn1neg   = <REAL_t *>(np.PyArray_DATA(model.wv_neg.vectors)) # why there is as ()
        c[0].cum_table = <np.uint32_t *>(np.PyArray_DATA(model.vocabulary.cum_table))
        c[0].cum_table_len = len(model.vocabulary.cum_table)
    if c[0].negative or c[0].sample:
        c[0].next_random = (2**24) * model.random.randint(0, 2**24) + model.random.randint(0, 2**24)

    # convert Python structures to primitive types, so we can release the GIL
    c[0].work = <REAL_t *>np.PyArray_DATA(_work)
    c[0].neu1 = <REAL_t *>np.PyArray_DATA(_neu1)

cdef unsigned long long fieldembed_token_neg_0X1_neat( 
    const REAL_t alpha, 
    const int size,
    const int negative, 
    np.uint32_t *cum_table, 
    unsigned long long cum_table_len, 

    const np.uint32_t indexes[MAX_SENTENCE_LEN], 
    int i, # right word loc_idx
    int j, # left  word loc_idx start
    int k, # left  word loc_idx end

    int use_head,                # 
    int use_sub,                 # 
    int use_hyper,

    map[int, REAL_t * ] syn0_map,
    map[int, np.uint32_t *] LookUp_map,
    map[int, np.uint32_t *] EndIdx_map,
    map[int, REAL_t *] LengInv_map,
    map[int, int] leng_max_map,

    REAL_t *syn1neg, 
    REAL_t *word_locks,

    REAL_t *neu1,  
    REAL_t *work,

    int cbow_mean, 
    unsigned long long next_random, 
    const int _compute_loss, 
    REAL_t *_running_training_loss_param) nogil:
    #===========================================================================================#

    # cdef long long a
    cdef long long row2
    cdef unsigned long long modulo = 281474976710655ULL
    
    cdef REAL_t label
    cdef REAL_t f_dot,  f,  g,  log_e_f_dot
    cdef REAL_t g2
    
    cdef int d, m  # d is for looping negative, m is for looping left words, 
    cdef int n # n is for looping left word's grain, shoud n be an int?
    cdef int left_word
    cdef int gs, ge
    cdef int proj_num = use_head + use_sub
    cdef np.uint32_t fld_idx
    cdef np.uint32_t target_index, word_index,  grain_index # should left_word be an int?

    cdef REAL_t count,  inv_count = 1.0
    cdef REAL_t word_lenginv = 1.0

    # Here word_index is np.uint32_t. very interesting
    word_index = indexes[i]  ########### S: get index for right token voc_idx
    # because indexes is np.int32_t

    #################################### S: Count the left tokens number
    count = <REAL_t>0.0
    for m in range(j, k):
        if m == i: # j, m, i, k are int
            continue
        else:
            count += ONEF
    if count > (<REAL_t>0.5):  # when using sg, count is 1. count is cw in word2vec.c
        inv_count = ONEF/count
    #################################### E: Count the left tokens number

    memset(neu1, 0, proj_num * size * cython.sizeof(REAL_t))
    
    fld_idx = -1

    #################################### S: calculate hProj from syn0
    if use_sub: # this is correct
        fld_idx = fld_idx + 1
        for m in range(j, k): # sg case: j = k; loop left tokens here
            if m == i:
                continue
            else:
                left_word  = indexes[m]                  # left_word: uint32 to int
                ###################################################################
                word_lenginv = LengInv_map[fld_idx][left_word] # word_lenginv: REAL_t
                gs = EndIdx_map[fld_idx][left_word-1]
                ge = EndIdx_map[fld_idx][left_word]
                for n in range(gs, ge):
                    # n is also np.uint_32
                    # should n be an int? just like m?
                    grain_index = LookUp_map[fld_idx][n] # syn0_1_LookUp is a np.uint_32
                    # grain_index is also np.uint_32
                    our_saxpy(&size, &word_lenginv, &syn0_map[fld_idx][grain_index * size],  &ONE, &neu1[fld_idx*size], &ONE)
                ###################################################################
        # if not sg:
        sscal(&size, &inv_count, &neu1[fld_idx*size], &ONE)  # (does this need BLAS-variants like saxpy? # no, you don't)
    #################################### E: calculate hProj from syn0

    #################################### E: calculate hProj from syn0
    if use_head: # this is correct
        fld_idx = fld_idx + 1
        # memset(neu1, 0, size * cython.sizeof(REAL_t))
        for m in range(j, k): # sg case: k = j+1; loop left tokens here
            if m == i: # j, m, i, k are int
                continue
            else:
                # cdef void our_saxpy_noblas(const int *N, const float *alpha, const float *X, const int *incX, float *Y, const int *incY) nogil:
                our_saxpy(&size, &ONEF, &syn0_map[fld_idx][indexes[m] * size], &ONE, &neu1[fld_idx*size], &ONE)
        # if not sg:
        sscal(&size, &inv_count, &neu1[fld_idx*size], &ONE)  # (does this need BLAS-variants like saxpy? # no, you don't)
    #################################### E: calculate hProj from syn0

    #################################### S: calculate hProj_grad and update syn1neg
    memset(work,  0, proj_num * size * cython.sizeof(REAL_t))

    for d in range(negative+1):
        # d is int
        if d == 0:
            target_index = word_index # word_index is vocab_index
            label = ONEF
        else:
            target_index = bisect_left(cum_table, (next_random >> 16) % cum_table[cum_table_len-1], 0, cum_table_len)
            next_random = (next_random * <unsigned long long>25214903917ULL + 11) & modulo
            if target_index == word_index:
                continue 
            label = <REAL_t>0.0

        row2 = target_index * size # target_index: np.uint32, size: int; row2: long long 
        ##########################################################################

        fld_idx = -1
        if use_sub:
            fld_idx = fld_idx + 1
            ################################################################
            f_dot = our_dot(&size, &neu1[fld_idx*size], &ONE, &syn1neg[row2], &ONE)
            if _compute_loss == 1: # TODO
                f_dot = (f_dot if d == 0  else -f_dot)
                if f_dot <= -MAX_EXP or f_dot >= MAX_EXP:
                    continue # this is still an issue
                log_e_f_dot = LOG_TABLE[<int>((f_dot + MAX_EXP) * (EXP_TABLE_SIZE / MAX_EXP / 2))]
                _running_training_loss_param[0] = _running_training_loss_param[0] - log_e_f_dot # it seems when using *i, to query it, use *[0]
            
            if f_dot <= -MAX_EXP or f_dot >= MAX_EXP:
                continue 
            f = EXP_TABLE[<int>((f_dot + MAX_EXP) * (EXP_TABLE_SIZE / MAX_EXP / 2))]
            
            g2 = (label - f) * alpha # Convert this to an array
            our_saxpy(&size, &g2, &syn1neg[row2], &ONE, &work[fld_idx*size], &ONE) # accumulate work
            ################################################################

        if use_head:
            fld_idx = fld_idx + 1
            f_dot = our_dot(&size, &neu1[fld_idx*size], &ONE, &syn1neg[row2], &ONE)
            
            if _compute_loss == 1: # TODO
                f_dot = (f_dot if d == 0  else -f_dot)
                if f_dot <= -MAX_EXP or f_dot >= MAX_EXP:
                    continue 
                log_e_f_dot = LOG_TABLE[<int>((f_dot + MAX_EXP) * (EXP_TABLE_SIZE / MAX_EXP / 2))]
                _running_training_loss_param[0] = _running_training_loss_param[0] - log_e_f_dot # it seems when using *i, to query it, use *[0]

            if f_dot <= -MAX_EXP or f_dot >= MAX_EXP:
                continue # quit: this is unreasonable.
            f = EXP_TABLE[<int>((f_dot + MAX_EXP) * (EXP_TABLE_SIZE / MAX_EXP / 2))]

            g = (label - f) * alpha
            our_saxpy(&size, &g,  &syn1neg[row2], &ONE, &work[fld_idx*size], &ONE) # accumulate work

        #########################################################################

        ##########################################################################
        fld_idx = -1
        if use_sub:
            fld_idx = fld_idx + 1
            our_saxpy(&size, &g2, &neu1[fld_idx*size], &ONE, &syn1neg[row2], &ONE)
        if use_head:
            fld_idx = fld_idx + 1
            our_saxpy(&size, &g,  &neu1[fld_idx*size], &ONE, &syn1neg[row2], &ONE)
        ##########################################################################
    #################################### E: calculate hProj_grad and update syn1neg

    #################################### S: update syn0 gradient
    if cbow_mean:  # use standard grad
        fld_idx = -1
        if use_sub:
            fld_idx = fld_idx + 1
            sscal(&size, &inv_count, &work[fld_idx*size], &ONE)  
        if use_head:
            fld_idx = fld_idx + 1
            sscal(&size, &inv_count, &work[fld_idx*size],  &ONE)  # (does this need BLAS-variants like saxpy?)

  
    fld_idx = -1
    
    if use_sub:
        fld_idx = fld_idx + 1
        for m in range(j, k): # sg case: j + 1 = k; loop left tokens here
            if m == i:
                continue
            else:
                ############### This four lines are important ###############
                # left_word  #  from uint32 to int 
                left_word = indexes[m] 
                word_lenginv = LengInv_map[fld_idx][left_word] # word_lenginv: REAL_t
                gs = EndIdx_map[fld_idx][left_word-1]     #  from uint32 to int 
                ge = EndIdx_map[fld_idx][left_word]       #  from uint32 to int 
                for n in range(gs, ge):                   #  n is int
                    grain_index = LookUp_map[fld_idx][n]  #  grain_index is uint
                    our_saxpy(&size, &word_lenginv,       &work[fld_idx*size], &ONE, &syn0_map[fld_idx][grain_index * size], &ONE) 

    if use_head:
        fld_idx = fld_idx + 1
        for m in range(j,k): 
            if m == i:
                continue
            else:
                our_saxpy(&size, &word_locks[indexes[m]], &work[fld_idx*size], &ONE, &syn0_map[fld_idx][ indexes[m] * size], &ONE)
      
    ################################### E: update syn0 gradient
    return next_random

def train_batch_fieldembed_0X1_neat(model, indexes, sentence_idx, alpha, _work, _neu1, compute_loss, subsampling = 1):

    cdef Word2VecConfig c
    cdef int i, j, k
    cdef int effective_words = 0, effective_sentences = 0
    cdef int sent_idx, idx_start, idx_end
    cdef int word_vocidx

    init_w2v_config_0X1_neat(&c, model, alpha, compute_loss, _work, _neu1) # this is the difference between sg and cbow
    
    if subsampling:
        vlookup = model.wv.vocab_values
        for sent_idx in range(len(sentence_idx)):
            # step1: get every sentence's idx_start and idx_end
            if sent_idx == 0:
                idx_start = 0
            else:
                idx_start = sentence_idx[sent_idx-1]
            idx_end = sentence_idx[sent_idx]

            # step2: loop every tokens in this sentence, drop special tokens and use downsampling
            for word_vocidx in indexes[idx_start: idx_end]:
                if word_vocidx <= 3:
                    continue
                if c.sample and vlookup[word_vocidx].sample_int < random_int32(&c.next_random):
                    continue
                # NOTICE: c.sentence_idx[0] = 0  # indices of the first sentence always start at 0
                # my sentence_idx is not started from 0
                c.indexes[effective_words] = word_vocidx
                effective_words +=1
                if effective_words == MAX_SENTENCE_LEN:
                    break  # TODO: log warning, tally overflow?

            # step3: add the new idx_end for this sentence, that is, the value of effective_words
            c.sentence_idx[effective_sentences] = effective_words
            effective_sentences += 1
            if effective_words == MAX_SENTENCE_LEN:
                break  # TODO: log warning, tally overflow?

    else:
        # In this case, we don't drop special tokens or use downsampling 
        effective_words = len(indexes)
        effective_sentences = len(sentence_idx) # different from the original sentence_idx and effective_sentences
        for i, item in enumerate(indexes):
            c.indexes[i] = item
        for i, item in enumerate(sentence_idx):
            c.sentence_idx[i] = item

    # precompute "reduced window" offsets in a single randint() call
    for i, item in enumerate(model.random.randint(0, c.window, effective_words)):
        c.reduced_windows[i] = item

    with nogil: # LESSION: you should notice this nogil, otherwise the threads are rubbish
        for sent_idx in range(effective_sentences):
            # idx_start and idx_end
            idx_end = c.sentence_idx[sent_idx]
            if sent_idx == 0:
                idx_start = 0
            else:
                idx_start = c.sentence_idx[sent_idx-1]

            for i in range(idx_start, idx_end):
                j = i - c.window + c.reduced_windows[i]
                if j < idx_start:
                    j = idx_start
                k = i + c.window + 1 - c.reduced_windows[i]
                if k > idx_end:
                    k = idx_end
                # print(j, i, k)

                if c.sg == 1:
                    for j in range(j, k): # change the first j to another name: such as t.
                        if j == i:
                            continue
                        c.next_random = fieldembed_token_neg_0X1_neat(c.alpha, c.size, c.negative, c.cum_table, c.cum_table_len, 
                            c.indexes, i, j, j + 1, 
                            c.use_head, c.use_sub,  c.use_hyper,
                            c.syn0_map,
                            c.LookUp_map, c.EndIdx_map, c.LengInv_map, c.leng_max_map,
                            c.syn1neg, c.word_locks, 
                            c.neu1, c.work, 
                            c.cbow_mean, c.next_random, c.compute_loss, &c.running_training_loss)
                else:
                    # build the batch here
                    c.next_random = fieldembed_token_neg_0X1_neat(c.alpha, c.size, c.negative, c.cum_table, c.cum_table_len, 
                            c.indexes, i, j, k, 
                            c.use_head, c.use_sub,  c.use_hyper,
                            c.syn0_map, c.LookUp_map, c.EndIdx_map, c.LengInv_map, c.leng_max_map,
                            c.syn1neg, c.word_locks, 
                            c.neu1, c.work, 
                            c.cbow_mean, c.next_random, c.compute_loss, &c.running_training_loss)

    model.running_training_loss = c.running_training_loss
    return effective_words

##############################################



cdef init_w2v_config(
    Word2VecConfig *c, 
    model, 
    alpha, 
    compute_loss, 
    _work, 
    _neu1):
    
    #===========================================================================================#
    
    ####################################################################### index and configuration
    cdef int i, fld_idx
    c[0].sg = model.sg
    c[0].negative = model.negative
    c[0].sample = (model.vocabulary.sample != 0)
    c[0].cbow_mean = model.standard_grad
    c[0].window = model.window
    c[0].workers = model.workers
    c[0].compute_loss = (1 if compute_loss else 0)
    c[0].running_training_loss = model.running_training_loss

    ####################################################################### sub_fields and head_fields
    fld_idx = -1 
    c[0].use_sub  = model.use_sub
    if c[0].use_sub:
        for i in range(c[0].use_sub):
            fld_idx  = fld_idx + 1
            c[0].syn0_map[fld_idx]    = <REAL_t *>(np.PyArray_DATA(model.field_sub[0][i][0].vectors))
            c[0].LookUp_map[fld_idx]  = <np.uint32_t *>(np.PyArray_DATA(model.field_sub[0][i][1]))
            c[0].EndIdx_map[fld_idx]  = <np.uint32_t *>(np.PyArray_DATA(model.field_sub[0][i][2]))
            c[0].LengInv_map[fld_idx] = <REAL_t *>(np.PyArray_DATA(model.field_sub[0][i][3])) 
            c[0].leng_max_map[fld_idx]= model.field_sub[0][i][4] 

    c[0].use_head = model.use_head 
    if c[0].use_head:
        fld_idx  = fld_idx + 1
        c[0].syn0_map[fld_idx] = <REAL_t *>(np.PyArray_DATA(model.field_head[0][1].vectors)) 
    
    ####################################################################### hyper_parameters
    c[0].word_locks = <REAL_t *>(np.PyArray_DATA(model.trainables.vectors_lockf))
    c[0].alpha = alpha
    c[0].size = model.wv.vector_size # there may not be any model.wv

    ####################################################################### negative embeddings
    if c[0].negative:
        c[0].syn1neg   = <REAL_t *>(np.PyArray_DATA(model.wv_neg.vectors)) # why there is as ()
        c[0].cum_table = <np.uint32_t *>(np.PyArray_DATA(model.vocabulary.cum_table))
        c[0].cum_table_len = len(model.vocabulary.cum_table)
    if c[0].negative or c[0].sample:
        c[0].next_random = (2**24) * model.random.randint(0, 2**24) + model.random.randint(0, 2**24)

    ####################################################################### use proj and grad vectors
    c[0].work = <REAL_t *>np.PyArray_DATA(_work)
    c[0].neu1 = <REAL_t *>np.PyArray_DATA(_neu1)


cdef unsigned long long fieldembed_negsamp( 
    const REAL_t alpha, 
    const int size,
    const int negative, 
    np.uint32_t *cum_table, 
    unsigned long long cum_table_len, 

    const np.uint32_t indexes[MAX_SENTENCE_LEN], 
    int i, # right word loc_idx
    int j, # left  word loc_idx start
    int k, # left  word loc_idx end

    int use_head,                # 
    int use_sub,                 # 
    int use_hyper,

    map[int, REAL_t * ] syn0_map,
    map[int, np.uint32_t *] LookUp_map,
    map[int, np.uint32_t *] EndIdx_map,
    map[int, REAL_t *] LengInv_map,
    map[int, int] leng_max_map,

    REAL_t *syn1neg, 
    REAL_t *word_locks,

    REAL_t *neu1,  
    REAL_t *work,

    int cbow_mean, 
    unsigned long long next_random, 
    const int _compute_loss, 
    REAL_t *_running_training_loss_param) nogil:
    #===========================================================================================#

    cdef int proj_num = use_head + use_sub
    cdef long long row2
    cdef unsigned long long modulo = 281474976710655ULL
    
    cdef REAL_t label
    cdef REAL_t f_dot,  f,  g,  log_e_f_dot
    cdef REAL_t g2
    cdef REAL_t g[proj_num]
    # d is for looping negative, m is for looping left words, 
    cdef int d, m  
    # n is for looping left word's grain, shoud n be an int?
    cdef int n 
    cdef int left_word
    cdef int gs, ge
    
    cdef np.uint32_t fld_idx
    cdef np.uint32_t target_index, word_index,  grain_index # should left_word be an int?

    cdef REAL_t count,  inv_count = 1.0
    cdef REAL_t word_lenginv = 1.0

    # here word_index is np.uint32_t. (very interesting
    # because indexes is np.int32_t
    word_index = indexes[i] 
    

    #################################### S: Count the left tokens number
    count = <REAL_t>0.0
    for m in range(j, k):
        # j, m, i, k are int
        if m == i: 
            continue
        else:
            count += ONEF

    # when using sg, count is 1. count is cw in word2vec.c
    if count > (<REAL_t>0.5):  
        inv_count = ONEF/count
    #################################### E: Count the left tokens number

    memset(neu1, 0, proj_num * size * cython.sizeof(REAL_t))
    
    fld_idx = -1

    #################################### S: calculate hProj from syn0
    # this is correct
    if use_sub: 
        fld_idx = fld_idx + 1
        # sg case: range(j, k) is range(j, j + 1)
        # loop left tokens here
        for m in range(j, k): 
            if m == i:
                continue
            else:
                # left_word: uint32 to int
                left_word  = indexes[m]                  
                ###################################################################
                # word_lenginv: REAL_t
                word_lenginv = LengInv_map[fld_idx][left_word] 
                gs = EndIdx_map[fld_idx][left_word-1]
                ge = EndIdx_map[fld_idx][left_word]
                for n in range(gs, ge):
                    # n is also np.uint_32
                    # should n be an int? just like m?
                    grain_index = LookUp_map[fld_idx][n] # syn0_1_LookUp is a np.uint_32
                    # grain_index is also np.uint_32
                    our_saxpy(&size, &word_lenginv, &syn0_map[fld_idx][grain_index * size],  &ONE, &neu1[fld_idx*size], &ONE)
                ###################################################################
        # if not sg:
        # (does this need BLAS-variants like saxpy? # no, you don't)
        sscal(&size, &inv_count, &neu1[fld_idx*size], &ONE)  
    #################################### E: calculate hProj from syn0

    
    #################################### S:
    # this is correct
    if use_head: 
        fld_idx = fld_idx + 1
        # memset(neu1, 0, size * cython.sizeof(REAL_t))
        # sg case: range(j, k) is range(j, j + 1)
        for m in range(j, k): 
            # j, m, i, k are int
            if m == i: 
                continue
            else:
                our_saxpy(&size, &ONEF, &syn0_map[fld_idx][indexes[m] * size], &ONE, &neu1[fld_idx*size], &ONE)
        # if not sg:
        # (does this need BLAS-variants like saxpy? # no, you don't)
        sscal(&size, &inv_count, &neu1[fld_idx*size], &ONE)
    #################################### E: 

    #################################### S: calculate hProj_grad and update syn1neg
    memset(work,  0, proj_num * size * cython.sizeof(REAL_t))

    for d in range(negative+1):
        # d is int
        if d == 0:
            # word_index is vocab_index
            target_index = word_index 
            label = ONEF
        else:
            target_index = bisect_left(cum_table, (next_random >> 16) % cum_table[cum_table_len-1], 0, cum_table_len)
            next_random = (next_random * <unsigned long long>25214903917ULL + 11) & modulo
            if target_index == word_index:
                continue 
            label = <REAL_t>0.0

        # target_index: np.uint32, size: int; row2: long long 
        row2 = target_index * size 
        ##########################################################################

        fld_idx = -1
        if use_sub:
            for i in range(use_sub):
                fld_idx = fld_idx + 1
                ################################################################
                f_dot = our_dot(&size, &neu1[fld_idx*size], &ONE, &syn1neg[row2], &ONE)
                if f_dot <= -MAX_EXP or f_dot >= MAX_EXP:
                    continue 

                f = EXP_TABLE[<int>((f_dot + MAX_EXP) * (EXP_TABLE_SIZE / MAX_EXP / 2))]
                g[fld_idx] = (label - f) * alpha 
                
                if _compute_loss == 1: 
                    # change f_dot according to the pair relationship: d
                    f_dot = (f_dot if d == 0  else -f_dot)
                    log_e_f_dot = LOG_TABLE[<int>((f_dot + MAX_EXP) * (EXP_TABLE_SIZE / MAX_EXP / 2))]
                    _running_training_loss_param[0] = _running_training_loss_param[0] - log_e_f_dot 

                our_saxpy(&size, &g[fld_idx], &syn1neg[row2], &ONE, &work[fld_idx*size], &ONE) # accumulate work
                ################################################################

        if use_head:
            fld_idx = fld_idx + 1
            f_dot = our_dot(&size, &neu1[fld_idx*size], &ONE, &syn1neg[row2], &ONE)
            if f_dot <= -MAX_EXP or f_dot >= MAX_EXP:
                    continue 

            f = EXP_TABLE[<int>((f_dot + MAX_EXP) * (EXP_TABLE_SIZE / MAX_EXP / 2))]
            g[fld_idx] = (label - f) * alpha 

            if _compute_loss == 1: 
                # change f_dot according to the pair relationship: d
                f_dot = (f_dot if d == 0  else -f_dot)
                log_e_f_dot = LOG_TABLE[<int>((f_dot + MAX_EXP) * (EXP_TABLE_SIZE / MAX_EXP / 2))]
                _running_training_loss_param[0] = _running_training_loss_param[0] - log_e_f_dot 

            our_saxpy(&size, &g[fld_idx], &syn1neg[row2], &ONE, &work[fld_idx*size], &ONE) 
        #########################################################################

        ##########################################################################
        fld_idx = -1
        if use_sub:
            fld_idx = fld_idx + 1
            our_saxpy(&size, &g[fld_idx], &neu1[fld_idx*size], &ONE, &syn1neg[row2], &ONE)
        if use_head:
            fld_idx = fld_idx + 1
            our_saxpy(&size, &g[fld_idx], &neu1[fld_idx*size], &ONE, &syn1neg[row2], &ONE)
        ##########################################################################
    #################################### E: calculate hProj_grad and update syn1neg

    #################################### S: update syn0 gradient
    # use standard grad
    if cbow_mean:  
        fld_idx = -1
        if use_sub:
            fld_idx = fld_idx + 1
            sscal(&size, &inv_count, &work[fld_idx*size], &ONE)  
        if use_head:
            fld_idx = fld_idx + 1
            sscal(&size, &inv_count, &work[fld_idx*size], &ONE)  

    fld_idx = -1
    if use_sub:
        fld_idx = fld_idx + 1
        # sg case: j + 1 = k; loop left tokens here
        for m in range(j, k): 
            if m == i:
                continue
            else:
                ############### This four lines are important ###############
                # left_word  #  from uint32 to int 
                left_word = indexes[m] 
                # word_lenginv: REAL_t
                word_lenginv = LengInv_map[fld_idx][left_word] 
                # from uint32 to int 
                gs = EndIdx_map[fld_idx][left_word-1]     
                # from uint32 to int 
                ge = EndIdx_map[fld_idx][left_word]  
                # n is int     
                for n in range(gs, ge):              
                    # grain_index is uint     
                    grain_index = LookUp_map[fld_idx][n] 
                    our_saxpy(&size, &word_lenginv, &work[fld_idx*size], &ONE, &syn0_map[fld_idx][grain_index * size], &ONE) 
    if use_head:
        fld_idx = fld_idx + 1
        for m in range(j,k): 
            if m == i:
                continue
            else:
                our_saxpy(&size, &word_locks[indexes[m]], &work[fld_idx*size], &ONE, &syn0_map[fld_idx][ indexes[m] * size], &ONE)
    ################################### E: update syn0 gradient
    
    return next_random

def train_batch_fieldembed_negsamp(model, indexes, sentence_idx, alpha, _work, _neu1, compute_loss, subsampling = 1):

    cdef Word2VecConfig c
    cdef int i, j, k
    cdef int effective_words = 0, effective_sentences = 0
    cdef int sent_idx, idx_start, idx_end
    cdef int word_vocidx

    init_w2v_config(&c, model, alpha, compute_loss, _work, _neu1) # this is the difference between sg and cbow
    
    if subsampling:
        vlookup = model.wv.vocab_values
        for sent_idx in range(len(sentence_idx)):
            # step1: get every sentence's idx_start and idx_end
            if sent_idx == 0:
                idx_start = 0
            else:
                idx_start = sentence_idx[sent_idx-1]
            idx_end = sentence_idx[sent_idx]

            # step2: loop every tokens in this sentence, drop special tokens and use downsampling
            for word_vocidx in indexes[idx_start: idx_end]:
                if word_vocidx <= 3:
                    continue
                if c.sample and vlookup[word_vocidx].sample_int < random_int32(&c.next_random):
                    continue
                # NOTICE: c.sentence_idx[0] = 0  # indices of the first sentence always start at 0
                # my sentence_idx is not started from 0
                c.indexes[effective_words] = word_vocidx
                effective_words +=1
                if effective_words == MAX_SENTENCE_LEN:
                    # TODO: log warning, tally overflow?
                    break  

            # step3: add the new idx_end for this sentence, that is, the value of effective_words
            c.sentence_idx[effective_sentences] = effective_words
            effective_sentences += 1
            if effective_words == MAX_SENTENCE_LEN:
                # TODO: log warning, tally overflow?
                break  

    else:
        # In this case, we don't drop special tokens or use downsampling 
        effective_words = len(indexes)
        # different from the original sentence_idx and effective_sentences
        effective_sentences = len(sentence_idx) 
        for i, item in enumerate(indexes):
            c.indexes[i] = item
        for i, item in enumerate(sentence_idx):
            c.sentence_idx[i] = item

    # precompute "reduced window" offsets in a single randint() call
    for i, item in enumerate(model.random.randint(0, c.window, effective_words)):
        c.reduced_windows[i] = item

    # LESSION: you should notice this nogil, otherwise the threads are rubbish
    with nogil: 
        for sent_idx in range(effective_sentences):
            # idx_start and idx_end
            idx_end = c.sentence_idx[sent_idx]
            if sent_idx == 0:
                idx_start = 0
            else:
                idx_start = c.sentence_idx[sent_idx-1]

            for i in range(idx_start, idx_end):
                j = i - c.window + c.reduced_windows[i]
                if j < idx_start:
                    j = idx_start
                k = i + c.window + 1 - c.reduced_windows[i]
                if k > idx_end:
                    k = idx_end
                # print(j, i, k)

                if c.sg == 1:
                    # change the first j to another name: such as t.
                    for j in range(j, k): 
                        if j == i:
                            continue
                        c.next_random = fieldembed_negsamp(c.alpha, c.size, c.negative, c.cum_table, c.cum_table_len, 
                            c.indexes, i, j, j + 1, 
                            c.use_head, c.use_sub,  c.use_hyper,
                            c.syn0_map,
                            c.LookUp_map, c.EndIdx_map, c.LengInv_map, c.leng_max_map,
                            c.syn1neg, c.word_locks, 
                            c.neu1, c.work, 
                            c.cbow_mean, c.next_random, c.compute_loss, &c.running_training_loss)
                else:
                    # build the batch here
                    c.next_random = fieldembed_negsamp(c.alpha, c.size, c.negative, c.cum_table, c.cum_table_len, 
                            c.indexes, i, j, k, 
                            c.use_head, c.use_sub,  c.use_hyper,
                            c.syn0_map, c.LookUp_map, c.EndIdx_map, c.LengInv_map, c.leng_max_map,
                            c.syn1neg, c.word_locks, 
                            c.neu1, c.work, 
                            c.cbow_mean, c.next_random, c.compute_loss, &c.running_training_loss)

    model.running_training_loss = c.running_training_loss
    return effective_words























def init():
    """Precompute function `sigmoid(x) = 1 / (1 + exp(-x))`, for x values discretized into table EXP_TABLE.
     Also calculate log(sigmoid(x)) into LOG_TABLE.

    Returns
    -------
    {0, 1, 2}
        Enumeration to signify underlying data type returned by the BLAS dot product calculation.
        0 signifies double, 1 signifies double, and 2 signifies that custom cython loops were used
        instead of BLAS.

    """
    global our_dot
    global our_saxpy

    cdef int i
    cdef float *x = [<float>10.0]
    cdef float *y = [<float>0.01]
    cdef float expected = <float>0.1
    cdef int size = 1
    cdef double d_res
    cdef float *p_res

    # build the sigmoid table
    for i in range(EXP_TABLE_SIZE):
        EXP_TABLE[i] = <REAL_t>exp((i / <REAL_t>EXP_TABLE_SIZE * 2 - 1) * MAX_EXP)
        EXP_TABLE[i] = <REAL_t>(EXP_TABLE[i] / (EXP_TABLE[i] + 1))
        LOG_TABLE[i] = <REAL_t>log( EXP_TABLE[i] )

    # check whether sdot returns double or float
    d_res = dsdot(&size, x, &ONE, y, &ONE)
    p_res = <float *>&d_res
    if abs(d_res - expected) < 0.0001:
        our_dot = our_dot_double
        our_saxpy = saxpy
        # our_saxpy = our_saxpy_noblas
        return 0  # double
    elif abs(p_res[0] - expected) < 0.0001:
        our_dot = our_dot_float
        our_saxpy = saxpy
        # our_saxpy = our_saxpy_noblas
        return 1  # float
    else:
        # neither => use cython loops, no BLAS
        # actually, the BLAS is so messed up we'll probably have segfaulted above and never even reach here
        our_dot = our_dot_noblas
        our_saxpy = our_saxpy_noblas
        return 2

FAST_VERSION = init()  # initialize the module
MAX_WORDS_IN_BATCH = MAX_SENTENCE_LEN