"""Unit tests for route53resolver endpoint-related APIs."""
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

import pytest

from moto import mock_route53resolver
from moto import settings
from moto.core import ACCOUNT_ID
from moto.core.utils import get_random_hex
from moto.ec2 import mock_ec2

TEST_REGION = "us-east-1" if settings.TEST_SERVER_MODE else "us-west-2"


def create_security_group(ec2_client):
    """Return a security group ID."""
    group_name = "RRUnitTests"

    # Does the security group already exist?
    groups = ec2_client.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [group_name]}]
    )

    # If so, we're done.  Otherwise, create it.
    if groups["SecurityGroups"]:
        return groups["SecurityGroups"][0]["GroupId"]

    response = ec2_client.create_security_group(
        Description="Security group used by unit tests", GroupName=group_name,
    )
    return response["GroupId"]


def create_vpc(ec2_client):
    """Return the ID for a valid VPC."""
    return ec2_client.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]


def create_subnets(ec2_client, vpc_id):
    """Returns the IDs for two valid subnets."""
    subnet_ids = []
    for cidr_block in ["10.0.1.0/24", "10.0.0.0/24"]:
        subnet_ids.append(
            ec2_client.create_subnet(
                VpcId=vpc_id, CidrBlock=cidr_block, AvailabilityZone=f"{TEST_REGION}a",
            )["Subnet"]["SubnetId"]
        )
    return subnet_ids


def create_test_endpoint(client, ec2_client, name=None, tags=None):
    """Create an endpoint that can be used for testing purposes.

    Can't be used for unit tests that need to know/test the arguments.
    """
    if not tags:
        tags = []
    random_num = get_random_hex(10)
    subnet_ids = create_subnets(ec2_client, create_vpc(ec2_client))
    resolver_endpoint = client.create_resolver_endpoint(
        CreatorRequestId=random_num,
        Name=name if name else "X" + random_num,
        SecurityGroupIds=[create_security_group(ec2_client)],
        Direction="INBOUND",
        IpAddresses=[
            {"SubnetId": subnet_ids[0], "Ip": "10.0.1.200"},
            {"SubnetId": subnet_ids[1], "Ip": "10.0.0.20"},
        ],
        Tags=tags,
    )
    return resolver_endpoint["ResolverEndpoint"]


@mock_route53resolver
def test_route53resolver_invalid_create_endpoint_args():
    """Test invalid arguments to the create_resolver_endpoint API."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    random_num = get_random_hex(10)

    # Verify ValidationException error messages are accumulated properly:
    #  - creator requestor ID that exceeds the allowed length of 255.
    #  - name that exceeds the allowed length of 64.
    #  - direction that's neither INBOUND or OUTBOUND.
    #  - more than 10 IP Address sets.
    #  - too many security group IDs.
    long_id = random_num * 25 + "123456"
    long_name = random_num * 6 + "abcde"
    too_many_security_groups = ["sg-" + get_random_hex(63)]
    bad_direction = "foo"
    too_many_ip_addresses = [{"SubnetId": f"{x}", "Ip": f"{x}" * 7} for x in range(11)]
    with pytest.raises(ClientError) as exc:
        client.create_resolver_endpoint(
            CreatorRequestId=long_id,
            Name=long_name,
            SecurityGroupIds=too_many_security_groups,
            Direction=bad_direction,
            IpAddresses=too_many_ip_addresses,
        )
    err = exc.value.response["Error"]
    assert err["Code"] == "ValidationException"
    assert "5 validation errors detected" in err["Message"]
    assert (
        f"Value '{long_id}' at 'creatorRequestId' failed to satisfy constraint: "
        f"Member must have length less than or equal to 255"
    ) in err["Message"]
    assert (
        f"Value '{too_many_security_groups}' at 'securityGroupIds' failed to "
        f"satisfy constraint: Member must have length less than or equal to 64"
    ) in err["Message"]
    assert (
        f"Value '{long_name}' at 'name' failed to satisfy constraint: "
        f"Member must have length less than or equal to 64"
    ) in err["Message"]
    assert (
        f"Value '{bad_direction}' at 'direction' failed to satisfy constraint: "
        f"Member must satisfy enum value set: [INBOUND, OUTBOUND]"
    ) in err["Message"]
    assert (
        f"Value '{too_many_ip_addresses}' at 'ipAddresses' failed to satisfy "
        f"constraint: Member must have length less than or equal to 10"
    ) in err["Message"]

    # Some single ValidationException errors ...
    bad_chars_in_name = "0@*3"
    ok_group_ids = ["sg-" + random_num]
    ok_ip_addrs = [{"SubnetId": f"{x}", "Ip": f"{x}" * 7} for x in range(10)]
    with pytest.raises(ClientError) as exc:
        client.create_resolver_endpoint(
            CreatorRequestId=random_num,
            Name=bad_chars_in_name,
            SecurityGroupIds=ok_group_ids,
            Direction="INBOUND",
            IpAddresses=ok_ip_addrs,
        )
    err = exc.value.response["Error"]
    assert err["Code"] == "ValidationException"
    assert "1 validation error detected" in err["Message"]
    assert (
        fr"Value '{bad_chars_in_name}' at 'name' failed to satisfy constraint: "
        fr"Member must satisfy regular expression pattern: "
        fr"^(?!^[0-9]+$)([a-zA-Z0-9-_' ']+)$"
    ) in err["Message"]

    subnet_too_long = [{"SubnetId": "a" * 33, "Ip": "1.2.3.4"}]
    with pytest.raises(ClientError) as exc:
        client.create_resolver_endpoint(
            CreatorRequestId=random_num,
            Name="X" + random_num,
            SecurityGroupIds=ok_group_ids,
            Direction="OUTBOUND",
            IpAddresses=subnet_too_long,
        )
    err = exc.value.response["Error"]
    assert err["Code"] == "ValidationException"
    assert "1 validation error detected" in err["Message"]
    assert (
        f"Value '{subnet_too_long}' at 'ipAddresses.subnetId' failed to "
        f"satisfy constraint: Member must have length less than or equal to 32"
    ) in err["Message"]


@mock_ec2
@mock_route53resolver
def test_route53resolver_bad_create_endpoint_subnets():
    """Test bad subnet scenarios for create_resolver_endpoint API."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    ec2_client = boto3.client("ec2", region_name=TEST_REGION)
    random_num = get_random_hex(10)

    # Need 2 IP addresses at the minimum.
    subnet_ids = create_subnets(ec2_client, create_vpc(ec2_client))
    with pytest.raises(ClientError) as exc:
        client.create_resolver_endpoint(
            CreatorRequestId=random_num,
            Name="X" + random_num,
            SecurityGroupIds=[f"sg-{random_num}"],
            Direction="INBOUND",
            IpAddresses=[{"SubnetId": subnet_ids[0], "Ip": "1.2.3.4"}],
        )
    err = exc.value.response["Error"]
    assert err["Code"] == "InvalidRequestException"
    assert "Resolver endpoint needs to have at least 2 IP addresses" in err["Message"]

    # Need an IP that's within in the subnet.
    bad_ip_addr = "1.2.3.4"
    with pytest.raises(ClientError) as exc:
        client.create_resolver_endpoint(
            CreatorRequestId=random_num,
            Name="X" + random_num,
            SecurityGroupIds=[f"sg-{random_num}"],
            Direction="INBOUND",
            IpAddresses=[
                {"SubnetId": subnet_ids[0], "Ip": bad_ip_addr},
                {"SubnetId": subnet_ids[1], "Ip": bad_ip_addr},
            ],
        )
    err = exc.value.response["Error"]
    assert err["Code"] == "InvalidRequestException"
    assert (
        f"IP address '{bad_ip_addr}' is either not in subnet "
        f"'{subnet_ids[0]}' CIDR range or is reserved"
    ) in err["Message"]

    # Bad subnet ID.
    with pytest.raises(ClientError) as exc:
        client.create_resolver_endpoint(
            CreatorRequestId=random_num,
            Name="X" + random_num,
            SecurityGroupIds=[f"sg-{random_num}"],
            Direction="INBOUND",
            IpAddresses=[
                {"SubnetId": "foo", "Ip": "1.2.3.4"},
                {"SubnetId": subnet_ids[1], "Ip": "1.2.3.4"},
            ],
        )
    err = exc.value.response["Error"]
    assert err["Code"] == "InvalidParameterException"
    assert "The subnet ID 'foo' does not exist" in err["Message"]

    # Can't reuse a ip address in a subnet.
    subnet_ids = create_subnets(ec2_client, create_vpc(ec2_client))
    with pytest.raises(ClientError) as exc:
        client.create_resolver_endpoint(
            CreatorRequestId="B" + random_num,
            Name="B" + random_num,
            SecurityGroupIds=[create_security_group(ec2_client)],
            Direction="INBOUND",
            IpAddresses=[
                {"SubnetId": subnet_ids[0], "Ip": "10.0.1.200"},
                {"SubnetId": subnet_ids[0], "Ip": "10.0.1.200"},
            ],
        )
    err = exc.value.response["Error"]
    assert err["Code"] == "ResourceExistsException"
    assert (
        f"The IP address '10.0.1.200' in subnet '{subnet_ids[0]}' is already in use"
    ) in err["Message"]


@mock_ec2
@mock_route53resolver
def test_route53resolver_bad_create_endpoint_security_groups():
    """Test bad security group scenarios for create_resolver_endpoint API."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    ec2_client = boto3.client("ec2", region_name=TEST_REGION)
    random_num = get_random_hex(10)

    subnet_ids = create_subnets(ec2_client, create_vpc(ec2_client))
    ip_addrs = [
        {"SubnetId": subnet_ids[0], "Ip": "10.0.1.200"},
        {"SubnetId": subnet_ids[1], "Ip": "10.0.0.20"},
    ]

    # Subnet must begin with "sg-".
    with pytest.raises(ClientError) as exc:
        client.create_resolver_endpoint(
            CreatorRequestId=random_num,
            Name="X" + random_num,
            SecurityGroupIds=["foo"],
            Direction="INBOUND",
            IpAddresses=ip_addrs,
        )
    err = exc.value.response["Error"]
    assert err["Code"] == "InvalidParameterException"
    assert (
        "Malformed security group ID: Invalid id: 'foo' (expecting 'sg-...')"
    ) in err["Message"]

    # Non-existent security group id.
    with pytest.raises(ClientError) as exc:
        client.create_resolver_endpoint(
            CreatorRequestId=random_num,
            Name="X" + random_num,
            SecurityGroupIds=["sg-abc"],
            Direction="INBOUND",
            IpAddresses=ip_addrs,
        )
    err = exc.value.response["Error"]
    assert err["Code"] == "ResourceNotFoundException"
    assert "The security group 'sg-abc' does not exist" in err["Message"]

    # Too many security group ids.
    with pytest.raises(ClientError) as exc:
        client.create_resolver_endpoint(
            CreatorRequestId=random_num,
            Name="X" + random_num,
            SecurityGroupIds=["sg-abc"] * 11,
            Direction="INBOUND",
            IpAddresses=ip_addrs,
        )
    err = exc.value.response["Error"]
    assert err["Code"] == "InvalidParameterException"
    assert "Maximum of 10 security groups are allowed" in err["Message"]


@mock_ec2
@mock_route53resolver
def test_route53resolver_create_resolver_endpoint():  # pylint: disable=too-many-locals
    """Test good create_resolver_endpoint API calls."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    ec2_client = boto3.client("ec2", region_name=TEST_REGION)
    random_num = get_random_hex(10)

    vpc_id = create_vpc(ec2_client)
    subnet_ids = create_subnets(ec2_client, vpc_id)
    ip_addrs = [
        {"SubnetId": subnet_ids[0], "Ip": "10.0.1.200"},
        {"SubnetId": subnet_ids[1], "Ip": "10.0.0.20"},
    ]
    security_group_id = create_security_group(ec2_client)

    creator_request_id = random_num
    name = "X" + random_num
    response = client.create_resolver_endpoint(
        CreatorRequestId=creator_request_id,
        Name=name,
        SecurityGroupIds=[security_group_id],
        Direction="INBOUND",
        IpAddresses=ip_addrs,
    )
    endpoint = response["ResolverEndpoint"]
    id_value = endpoint["Id"]
    assert id_value.startswith("rslvr-in-")
    assert endpoint["CreatorRequestId"] == creator_request_id
    assert (
        endpoint["Arn"]
        == f"arn:aws:route53resolver:{TEST_REGION}:{ACCOUNT_ID}:resolver-endpoint/{id_value}"
    )
    assert endpoint["Name"] == name
    assert endpoint["SecurityGroupIds"] == [security_group_id]
    assert endpoint["Direction"] == "INBOUND"
    assert endpoint["IpAddressCount"] == 2
    assert endpoint["HostVPCId"] == vpc_id
    assert endpoint["Status"] == "OPERATIONAL"
    assert "Creating the Resolver Endpoint" in endpoint["StatusMessage"]

    time_format = "%Y-%m-%dT%H:%M:%S.%f+00:00"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    creation_time = datetime.strptime(endpoint["CreationTime"], time_format)
    creation_time = creation_time.replace(tzinfo=None)
    assert creation_time <= now

    modification_time = datetime.strptime(endpoint["ModificationTime"], time_format)
    modification_time = modification_time.replace(tzinfo=None)
    assert modification_time <= now


@mock_ec2
@mock_route53resolver
def test_route53resolver_other_create_resolver_endpoint_errors():
    """Test good delete_resolver_endpoint API calls."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    ec2_client = boto3.client("ec2", region_name=TEST_REGION)

    # Create a good endpoint that we can use to test.
    created_endpoint = create_test_endpoint(client, ec2_client)
    request_id = created_endpoint["CreatorRequestId"]

    # Attempt to create another endpoint with the same creator request id.
    vpc_id = create_vpc(ec2_client)
    subnet_ids = create_subnets(ec2_client, vpc_id)
    with pytest.raises(ClientError) as exc:
        client.create_resolver_endpoint(
            CreatorRequestId=created_endpoint["CreatorRequestId"],
            Name="X" + get_random_hex(10),
            SecurityGroupIds=created_endpoint["SecurityGroupIds"],
            Direction="INBOUND",
            IpAddresses=[
                {"SubnetId": subnet_ids[0], "Ip": "10.0.1.200"},
                {"SubnetId": subnet_ids[1], "Ip": "10.0.0.20"},
            ],
        )
    err = exc.value.response["Error"]
    assert err["Code"] == "ResourceExistsException"
    assert (
        f"Resolver endpoint with creator request ID '{request_id}' already exists"
    ) in err["Message"]

    # Too many endpoints.
    random_num = get_random_hex(10)
    for idx in range(4):
        create_test_endpoint(client, ec2_client, name=f"A{idx}-{random_num}")
    with pytest.raises(ClientError) as exc:
        create_test_endpoint(client, ec2_client, name=f"A5-{random_num}")
    err = exc.value.response["Error"]
    assert err["Code"] == "LimitExceededException"
    assert f"Account '{ACCOUNT_ID}' has exceeded 'max-endpoints" in err["Message"]


@mock_ec2
@mock_route53resolver
def test_route53resolver_delete_resolver_endpoint():
    """Test good delete_resolver_endpoint API calls."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    ec2_client = boto3.client("ec2", region_name=TEST_REGION)
    created_endpoint = create_test_endpoint(client, ec2_client)

    # Now delete the resolver endpoint and verify the response.
    response = client.delete_resolver_endpoint(
        ResolverEndpointId=created_endpoint["Id"]
    )
    endpoint = response["ResolverEndpoint"]
    assert endpoint["CreatorRequestId"] == created_endpoint["CreatorRequestId"]
    assert endpoint["Id"] == created_endpoint["Id"]
    assert endpoint["Arn"] == created_endpoint["Arn"]
    assert endpoint["Name"] == created_endpoint["Name"]
    assert endpoint["SecurityGroupIds"] == created_endpoint["SecurityGroupIds"]
    assert endpoint["Direction"] == created_endpoint["Direction"]
    assert endpoint["IpAddressCount"] == created_endpoint["IpAddressCount"]
    assert endpoint["HostVPCId"] == created_endpoint["HostVPCId"]
    assert endpoint["Status"] == "DELETING"
    assert "Deleting" in endpoint["StatusMessage"]
    assert endpoint["CreationTime"] == created_endpoint["CreationTime"]


@mock_route53resolver
def test_route53resolver_bad_delete_resolver_endpoint():
    """Test delete_resolver_endpoint API calls with a bad ID."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    random_num = get_random_hex(10)

    # Use a resolver endpoint id that is too long.
    long_id = "0123456789" * 6 + "xxxxx"
    with pytest.raises(ClientError) as exc:
        client.delete_resolver_endpoint(ResolverEndpointId=long_id)
    err = exc.value.response["Error"]
    assert err["Code"] == "ValidationException"
    assert "1 validation error detected" in err["Message"]
    assert (
        f"Value '{long_id}' at 'resolverEndpointId' failed to satisfy "
        f"constraint: Member must have length less than or equal to 64"
    ) in err["Message"]

    # Delete a non-existent resolver endpoint.
    with pytest.raises(ClientError) as exc:
        client.delete_resolver_endpoint(ResolverEndpointId=random_num)
    err = exc.value.response["Error"]
    assert err["Code"] == "ResourceNotFoundException"
    assert f"Resolver endpoint with ID '{random_num}' does not exist" in err["Message"]


@mock_ec2
@mock_route53resolver
def test_route53resolver_get_resolver_endpoint():
    """Test good get_resolver_endpoint API calls."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    ec2_client = boto3.client("ec2", region_name=TEST_REGION)
    created_endpoint = create_test_endpoint(client, ec2_client)

    # Now get the resolver endpoint and verify the response.
    response = client.get_resolver_endpoint(ResolverEndpointId=created_endpoint["Id"])
    endpoint = response["ResolverEndpoint"]
    assert endpoint["CreatorRequestId"] == created_endpoint["CreatorRequestId"]
    assert endpoint["Id"] == created_endpoint["Id"]
    assert endpoint["Arn"] == created_endpoint["Arn"]
    assert endpoint["Name"] == created_endpoint["Name"]
    assert endpoint["SecurityGroupIds"] == created_endpoint["SecurityGroupIds"]
    assert endpoint["Direction"] == created_endpoint["Direction"]
    assert endpoint["IpAddressCount"] == created_endpoint["IpAddressCount"]
    assert endpoint["HostVPCId"] == created_endpoint["HostVPCId"]
    assert endpoint["Status"] == created_endpoint["Status"]
    assert endpoint["StatusMessage"] == created_endpoint["StatusMessage"]
    assert endpoint["CreationTime"] == created_endpoint["CreationTime"]
    assert endpoint["ModificationTime"] == created_endpoint["ModificationTime"]


@mock_route53resolver
def test_route53resolver_bad_get_resolver_endpoint():
    """Test get_resolver_endpoint API calls with a bad ID."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    random_num = get_random_hex(10)

    # Use a resolver endpoint id that is too long.
    long_id = "0123456789" * 6 + "xxxxx"
    with pytest.raises(ClientError) as exc:
        client.get_resolver_endpoint(ResolverEndpointId=long_id)
    err = exc.value.response["Error"]
    assert err["Code"] == "ValidationException"
    assert "1 validation error detected" in err["Message"]
    assert (
        f"Value '{long_id}' at 'resolverEndpointId' failed to satisfy "
        f"constraint: Member must have length less than or equal to 64"
    ) in err["Message"]

    # Delete a non-existent resolver endpoint.
    with pytest.raises(ClientError) as exc:
        client.get_resolver_endpoint(ResolverEndpointId=random_num)
    err = exc.value.response["Error"]
    assert err["Code"] == "ResourceNotFoundException"
    assert f"Resolver endpoint with ID '{random_num}' does not exist" in err["Message"]


@mock_ec2
@mock_route53resolver
def test_route53resolver_update_resolver_endpoint():
    """Test good update_resolver_endpoint API calls."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    ec2_client = boto3.client("ec2", region_name=TEST_REGION)
    created_endpoint = create_test_endpoint(client, ec2_client)

    # Now update the resolver endpoint name and verify the response.
    new_name = "NewName" + get_random_hex(6)
    response = client.update_resolver_endpoint(
        ResolverEndpointId=created_endpoint["Id"], Name=new_name,
    )
    endpoint = response["ResolverEndpoint"]
    assert endpoint["CreatorRequestId"] == created_endpoint["CreatorRequestId"]
    assert endpoint["Id"] == created_endpoint["Id"]
    assert endpoint["Arn"] == created_endpoint["Arn"]
    assert endpoint["Name"] == new_name
    assert endpoint["SecurityGroupIds"] == created_endpoint["SecurityGroupIds"]
    assert endpoint["Direction"] == created_endpoint["Direction"]
    assert endpoint["IpAddressCount"] == created_endpoint["IpAddressCount"]
    assert endpoint["HostVPCId"] == created_endpoint["HostVPCId"]
    assert endpoint["Status"] == created_endpoint["Status"]
    assert endpoint["StatusMessage"] == created_endpoint["StatusMessage"]
    assert endpoint["CreationTime"] == created_endpoint["CreationTime"]
    assert endpoint["ModificationTime"] != created_endpoint["ModificationTime"]


@mock_route53resolver
def test_route53resolver_bad_update_resolver_endpoint():
    """Test update_resolver_endpoint API calls with a bad ID."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    random_num = get_random_hex(10)
    random_name = "Z" + get_random_hex(10)

    # Use a resolver endpoint id that is too long.
    long_id = "0123456789" * 6 + "xxxxx"
    with pytest.raises(ClientError) as exc:
        client.update_resolver_endpoint(ResolverEndpointId=long_id, Name=random_name)
    err = exc.value.response["Error"]
    assert err["Code"] == "ValidationException"
    assert "1 validation error detected" in err["Message"]
    assert (
        f"Value '{long_id}' at 'resolverEndpointId' failed to satisfy "
        f"constraint: Member must have length less than or equal to 64"
    ) in err["Message"]

    # Delete a non-existent resolver endpoint.
    with pytest.raises(ClientError) as exc:
        client.update_resolver_endpoint(ResolverEndpointId=random_num, Name=random_name)
    err = exc.value.response["Error"]
    assert err["Code"] == "ResourceNotFoundException"
    assert f"Resolver endpoint with ID '{random_num}' does not exist" in err["Message"]


@mock_ec2
@mock_route53resolver
def test_route53resolver_list_resolver_endpoint_ip_addresses():
    """Test good list_resolver_endpoint_ip_addresses API calls."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    ec2_client = boto3.client("ec2", region_name=TEST_REGION)
    random_num = get_random_hex(10)

    subnet_ids = create_subnets(ec2_client, create_vpc(ec2_client))
    response = client.create_resolver_endpoint(
        CreatorRequestId="B" + random_num,
        Name="B" + random_num,
        SecurityGroupIds=[create_security_group(ec2_client)],
        Direction="INBOUND",
        IpAddresses=[
            {"SubnetId": subnet_ids[0], "Ip": "10.0.1.200"},
            {"SubnetId": subnet_ids[1], "Ip": "10.0.0.20"},
            {"SubnetId": subnet_ids[0], "Ip": "10.0.1.201"},
        ],
    )
    endpoint_id = response["ResolverEndpoint"]["Id"]
    response = client.list_resolver_endpoint_ip_addresses(
        ResolverEndpointId=endpoint_id
    )
    assert len(response["IpAddresses"]) == 3
    assert response["MaxResults"] == 10

    # Set max_results to return 1 address, use next_token to get remaining.
    response = client.list_resolver_endpoint_ip_addresses(
        ResolverEndpointId=endpoint_id, MaxResults=1
    )
    ip_addresses = response["IpAddresses"]
    assert len(ip_addresses) == 1
    assert response["MaxResults"] == 1
    assert "NextToken" in response
    assert ip_addresses[0]["IpId"].startswith("rni-")
    assert ip_addresses[0]["SubnetId"] == subnet_ids[0]
    assert ip_addresses[0]["Ip"] == "10.0.1.200"
    assert ip_addresses[0]["Status"] == "ATTACHED"
    assert ip_addresses[0]["StatusMessage"] == "This IP address is operational."
    assert "CreationTime" in ip_addresses[0]
    assert "ModificationTime" in ip_addresses[0]

    response = client.list_resolver_endpoint_ip_addresses(
        ResolverEndpointId=endpoint_id, NextToken=response["NextToken"]
    )
    assert len(response["IpAddresses"]) == 2
    assert response["MaxResults"] == 10
    assert "NextToken" not in response


@mock_ec2
@mock_route53resolver
def test_route53resolver_bad_list_resolver_endpoint_ip_addresses():
    """Test bad list_resolver_endpoint_ip_addresses API calls."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    ec2_client = boto3.client("ec2", region_name=TEST_REGION)

    # Bad endpoint id.
    with pytest.raises(ClientError) as exc:
        client.list_resolver_endpoint_ip_addresses(ResolverEndpointId="foo")
    err = exc.value.response["Error"]
    assert err["Code"] == "ResourceNotFoundException"
    assert "Resolver endpoint with ID 'foo' does not exist" in err["Message"]

    # Good endpoint id, but bad max_results.
    random_num = get_random_hex(10)
    response = create_test_endpoint(client, ec2_client, name=f"A-{random_num}")
    with pytest.raises(ClientError) as exc:
        client.list_resolver_endpoint_ip_addresses(
            ResolverEndpointId=response["Id"], MaxResults=250
        )
    err = exc.value.response["Error"]
    assert err["Code"] == "ValidationException"
    assert "1 validation error detected" in err["Message"]
    assert (
        "Value '250' at 'maxResults' failed to satisfy constraint: Member "
        "must have length less than or equal to 100"
    ) in err["Message"]


@mock_ec2
@mock_route53resolver
def test_route53resolver_list_resolver_endpoints():
    """Test good list_resolver_endpoint API calls."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    ec2_client = boto3.client("ec2", region_name=TEST_REGION)
    random_num = get_random_hex(10)

    # List endpoints when there are none.
    response = client.list_resolver_endpoints()
    assert len(response["ResolverEndpoints"]) == 0
    assert response["MaxResults"] == 10
    assert "NextToken" not in response

    # Create 5 endpoints, verify all 5 are listed when no filters, max_results.
    for idx in range(4):
        create_test_endpoint(client, ec2_client, name=f"A{idx}-{random_num}")
    response = client.list_resolver_endpoints()
    endpoints = response["ResolverEndpoints"]
    assert len(endpoints) == 4
    assert response["MaxResults"] == 10
    for idx in range(4):
        assert endpoints[idx]["Name"].startswith(f"A{idx}")

    # Set max_results to return 1 endpoint, use next_token to get remaining 3.
    response = client.list_resolver_endpoints(MaxResults=1)
    endpoints = response["ResolverEndpoints"]
    assert len(endpoints) == 1
    assert response["MaxResults"] == 1
    assert "NextToken" in response
    assert endpoints[0]["Name"].startswith("A0")

    response = client.list_resolver_endpoints(NextToken=response["NextToken"])
    endpoints = response["ResolverEndpoints"]
    assert len(endpoints) == 3
    assert response["MaxResults"] == 10
    assert "NextToken" not in response
    for idx, endpoint in enumerate(endpoints):
        assert endpoint["Name"].startswith(f"A{idx + 1}")


@mock_ec2
@mock_route53resolver
def test_route53resolver_bad_list_resolver_endpoints():
    """Test bad list_resolver_endpoints API calls."""
    client = boto3.client("route53resolver", region_name=TEST_REGION)
    ec2_client = boto3.client("ec2", region_name=TEST_REGION)

    # Bad max_results.
    random_num = get_random_hex(10)
    create_test_endpoint(client, ec2_client, name=f"A-{random_num}")
    with pytest.raises(ClientError) as exc:
        client.list_resolver_endpoints(MaxResults=250)
    err = exc.value.response["Error"]
    assert err["Code"] == "ValidationException"
    assert "1 validation error detected" in err["Message"]
    assert (
        "Value '250' at 'maxResults' failed to satisfy constraint: Member "
        "must have length less than or equal to 100"
    ) in err["Message"]
