#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#


import sys

from destination_customer_io import DestinationCustomerIO

if __name__ == "__main__":
    DestinationCustomerIO().run(sys.argv[1:])
