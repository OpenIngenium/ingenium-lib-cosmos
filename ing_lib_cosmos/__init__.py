import logging    
# add NullHandler to suppress logging error when propagate is False for the log of this package
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = []

__version__ = '0.1.0'



def get_version():
    """
    A getter for the package version
    Args:
    Returns:
        The current version
    """
    return __version__