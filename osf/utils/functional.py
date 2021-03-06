import collections

# Function courtesy of @brianjgeiger and @abought
def rapply(data, func, *args, **kwargs):
    """Recursively apply a function to all values in an iterable
    :param dict | list | basestring data: iterable to apply func to
    :param function func:
    """
    if isinstance(data, collections.Mapping):
        return {
            key: rapply(value, func, *args, **kwargs)
            for key, value in data.iteritems()
        }
    elif isinstance(data, collections.Iterable) and not isinstance(data, basestring):
        desired_type = type(data)
        return desired_type(
            rapply(item, func, *args, **kwargs) for item in data
        )
    else:
        return func(data, *args, **kwargs)
