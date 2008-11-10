import numpy as np

class FastFinder(object):
    """fast search by use of a cached, sorted copy of the original data

    Parameters
    ----------
    values1d : 1D array
      The input data which is sorted and stored for indexing
    """
    def __init__(self,values1d):
        values1d = np.atleast_1d( values1d )
        assert len(values1d.shape)==1, 'only 1D arrays supported'
        self.idxs = np.argsort( values1d )
        self.sorted = values1d[ self.idxs ]
    def get_idxs_of_equal(self,testval):
        """performs fast search on sorted data

        Parameters
        ----------
        testval : scalar
          The value to find the indices of

        Returns
        -------
        result : array
          The indices into the original values1d array

        Examples
        --------

        >>> a = np.array([1,2,3,3,2,1,2.3])
        >>> af = FastFinder(a)
        >>> bs = [0, 1, 2, 1.1]
        >>> for b in bs:
        ...     af.get_idxs_of_equal(b).tolist()
        ...
        []
        [0, 5]
        [1, 4]
        []

        """
        testval = np.asarray(testval)
        assert len( testval.shape)==0, 'can only find equality of a scalar'

        left_idxs = self.sorted.searchsorted( testval, side='left' )
        right_idxs = self.sorted.searchsorted( testval, side='right' )

        this_idxs = self.idxs[left_idxs:right_idxs]
        return this_idxs
    def get_first_idx_of_assumed_equal(self,testval):
        """performs fast search on sorted data

        Parameters
        ----------
        testval : scalar
          The value to find the indices of

        Returns
        -------
        result : array
          The indices into the original values1d array

        Examples
        --------

        >>> a = np.array([1,2,3,3,2,1,2.3])
        >>> af = FastFinder(a)
        >>> bs = [0, 1, 2, 1.1]
        >>> for b in bs:
        ...     af.get_first_idx_of_assumed_equal(b)
        ...
        0
        0
        1
        1

        """
        testval = np.asarray(testval)
        assert len( testval.shape)==0, 'can only find equality of a scalar'

        left_idx = self.sorted.searchsorted( testval, side='left' )

        this_idx = self.idxs[left_idx]
        return this_idx

def get_contig_chunk_idxs( arr ):
    """get indices of contiguous chunks

    Parameters
    ----------
    arr : 1D array

    Results
    -------
    list_of_startstops : list of 2-tuples
        A list of tuples, where each tuple is (start_idx,stop_idx) of arr
    """
    #ADS print 'arr',arr
    diff = arr[1:]-arr[:-1]
    #ADS print 'diff',diff
    non_one = diff != 1
    #ADS print 'non_one',non_one

    non_one = np.ma.array(non_one).filled()
    #ADS print 'non_one',non_one

    idxs = np.nonzero(non_one)[0]
    #ADS print 'idxs',idxs
    chunk_idxs = []
    prev_idx = 0
    for idx in idxs:
        next_idx = idx+1
        #ADS print 'idx, prev_idx, next_idx',idx, prev_idx, next_idx
        data_chunk = np.ma.array(arr[prev_idx:next_idx])
        #ADS print 'data_chunk',data_chunk
        if not np.any(data_chunk.mask):
            chunk_idxs.append( (prev_idx, next_idx) )
        else:
            #ADS print 'skipped!'
            pass
        prev_idx = next_idx
    chunk_idxs.append( (prev_idx,len(arr)) )
    return chunk_idxs

def test_get_contig_chunk_idxs_1():
    input = np.array( [1,2,3,4,5,  11,12,13,14,15,16, 0, 5, -1], dtype=float)
    expected = [ (0,5),
                 (5,11),
                 (11,12),
                 (12,13),
                 (13,14),
                 ]
    actual = get_contig_chunk_idxs(input)
    for i in range(len(expected)):
        start, stop = expected[i]
        assert (start,stop)== actual[i]

def test_get_contig_chunk_idxs_2():
    input = np.array( [np.nan,np.nan,3,4,5,  np.nan,12,13,14,15,16, 0, 5, -1], dtype=float)
    input = np.ma.masked_where( np.isnan(input), input )
    expected = [ (2,5),
                 (6,11),
                 (11,12),
                 (12,13),
                 (13,14),
                 ]
    actual = get_contig_chunk_idxs(input)
    for i in range(len(expected)):
        start, stop = expected[i]
        assert (start,stop)== actual[i]

def test_fast_finder():
    a = np.array([1,2,3,3,2,1,2.3])
    bs = [0, 1, 2, 1.1]
    af = FastFinder(a)
    for b in bs:
        idxs1 = af.get_idxs_of_equal(b)
        idxs2 = np.nonzero(a==b)[0]
        assert idxs1.shape == idxs2.shape
        assert np.allclose( idxs1, idxs2 )
    for b in bs:
        idx1 = af.get_first_idx_of_assumed_equal(b)
        aval = a[idx1]
        if aval != b:
            if b in a:
                raise ValueError('b in a, but not found')
