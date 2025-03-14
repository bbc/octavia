# Copyright 2015 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#

from cryptography import fernet
from oslo_config import cfg
from oslo_db import exception as odb_exceptions
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import uuidutils
import sqlalchemy
from sqlalchemy.orm import exc
from taskflow import task
from taskflow.types import failure

from octavia.api.drivers import utils as provider_utils
from octavia.common import constants
from octavia.common import data_models
import octavia.common.tls_utils.cert_parser as cert_parser
from octavia.common import utils
from octavia.controller.worker import task_utils as task_utilities
from octavia.db import api as db_apis
from octavia.db import repositories as repo

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class BaseDatabaseTask(task.Task):
    """Base task to load drivers common to the tasks."""

    def __init__(self, **kwargs):
        self.repos = repo.Repositories()
        self.amphora_repo = repo.AmphoraRepository()
        self.health_mon_repo = repo.HealthMonitorRepository()
        self.listener_repo = repo.ListenerRepository()
        self.loadbalancer_repo = repo.LoadBalancerRepository()
        self.vip_repo = repo.VipRepository()
        self.member_repo = repo.MemberRepository()
        self.pool_repo = repo.PoolRepository()
        self.amp_health_repo = repo.AmphoraHealthRepository()
        self.l7policy_repo = repo.L7PolicyRepository()
        self.l7rule_repo = repo.L7RuleRepository()
        self.task_utils = task_utilities.TaskUtils()
        super().__init__(**kwargs)

    def _delete_from_amp_health(self, amphora_id):
        """Delete the amphora_health record for an amphora.

        :param amphora_id: The amphora id to delete
        """
        LOG.debug('Disabling health monitoring on amphora: %s', amphora_id)
        try:
            self.amp_health_repo.delete(db_apis.get_session(),
                                        amphora_id=amphora_id)
        except (sqlalchemy.orm.exc.NoResultFound,
                sqlalchemy.orm.exc.UnmappedInstanceError):
            LOG.debug('No existing amphora health record to delete '
                      'for amphora: %s, skipping.', amphora_id)

    def _mark_amp_health_busy(self, amphora_id):
        """Mark the amphora_health record busy for an amphora.

        :param amphora_id: The amphora id to mark busy
        """
        LOG.debug('Marking health monitoring busy on amphora: %s', amphora_id)
        try:
            self.amp_health_repo.update(db_apis.get_session(),
                                        amphora_id=amphora_id,
                                        busy=True)
        except (sqlalchemy.orm.exc.NoResultFound,
                sqlalchemy.orm.exc.UnmappedInstanceError):
            LOG.debug('No existing amphora health record to mark busy '
                      'for amphora: %s, skipping.', amphora_id)


class CreateAmphoraInDB(BaseDatabaseTask):
    """Task to create an initial amphora in the Database."""

    def execute(self, *args, loadbalancer_id=None, **kwargs):
        """Creates an pending create amphora record in the database.

        :returns: The created amphora object
        """

        amphora = self.amphora_repo.create(db_apis.get_session(),
                                           id=uuidutils.generate_uuid(),
                                           load_balancer_id=loadbalancer_id,
                                           status=constants.PENDING_CREATE,
                                           cert_busy=False)

        LOG.info("Created Amphora in DB with id %s", amphora.id)
        return amphora.id

    def revert(self, result, *args, **kwargs):
        """Revert by storing the amphora in error state in the DB

        In a future version we might change the status to DELETED
        if deleting the amphora was successful

        :param result: Id of created amphora.
        :returns: None
        """

        if isinstance(result, failure.Failure):
            # This task's execute failed, so nothing needed to be done to
            # revert
            return

        # At this point the revert is being called because another task
        # executed after this failed so we will need to do something and
        # result is the amphora's id

        LOG.warning("Reverting create amphora in DB for amp id %s ", result)

        # Delete the amphora for now. May want to just update status later
        try:
            self.amphora_repo.delete(db_apis.get_session(), id=result)
        except Exception as e:
            LOG.error("Failed to delete amphora %(amp)s "
                      "in the database due to: "
                      "%(except)s", {'amp': result, 'except': str(e)})


class MarkLBAmphoraeDeletedInDB(BaseDatabaseTask):
    """Task to mark a list of amphora deleted in the Database."""

    def execute(self, loadbalancer):
        """Update load balancer's amphorae statuses to DELETED in the database.

        :param loadbalancer: The load balancer which amphorae should be
               marked DELETED.
        :returns: None
        """
        db_lb = self.repos.load_balancer.get(
            db_apis.get_session(), id=loadbalancer[constants.LOADBALANCER_ID])
        for amp in db_lb.amphorae:
            LOG.debug("Marking amphora %s DELETED ", amp.id)
            self.amphora_repo.update(db_apis.get_session(),
                                     id=amp.id, status=constants.DELETED)


class DeleteHealthMonitorInDB(BaseDatabaseTask):
    """Delete the health monitor in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, health_mon):
        """Delete the health monitor in DB

        :param health_mon: The health monitor which should be deleted
        :returns: None
        """

        LOG.debug("DB delete health monitor: %s ",
                  health_mon[constants.HEALTHMONITOR_ID])
        try:
            self.health_mon_repo.delete(
                db_apis.get_session(),
                id=health_mon[constants.HEALTHMONITOR_ID])
        except exc.NoResultFound:
            # ignore if the HealthMonitor was not found
            pass

    def revert(self, health_mon, *args, **kwargs):
        """Mark the health monitor ERROR since the mark active couldn't happen

        :param health_mon: The health monitor which couldn't be deleted
        :returns: None
        """

        LOG.warning("Reverting mark health monitor delete in DB "
                    "for health monitor with id %s",
                    health_mon[constants.HEALTHMONITOR_ID])
        self.health_mon_repo.update(db_apis.get_session(),
                                    id=health_mon[constants.HEALTHMONITOR_ID],
                                    provisioning_status=constants.ERROR)


class DeleteHealthMonitorInDBByPool(DeleteHealthMonitorInDB):
    """Delete the health monitor in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, pool_id):
        """Delete the health monitor in the DB.

        :param pool_id: ID of pool which health monitor should be deleted.
        :returns: None
        """
        db_pool = self.pool_repo.get(db_apis.get_session(),
                                     id=pool_id)
        provider_hm = provider_utils.db_HM_to_provider_HM(
            db_pool.health_monitor).to_dict()
        super().execute(
            provider_hm)

    def revert(self, pool_id, *args, **kwargs):
        """Mark the health monitor ERROR since the mark active couldn't happen

        :param pool_id: ID of pool which health monitor couldn't be deleted
        :returns: None
        """
        db_pool = self.pool_repo.get(db_apis.get_session(),
                                     id=pool_id)
        provider_hm = provider_utils.db_HM_to_provider_HM(
            db_pool.health_monitor).to_dict()
        super().revert(
            provider_hm, *args, **kwargs)


class DeleteMemberInDB(BaseDatabaseTask):
    """Delete the member in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, member):
        """Delete the member in the DB

        :param member: The member to be deleted
        :returns: None
        """

        LOG.debug("DB delete member for id: %s ", member[constants.MEMBER_ID])
        self.member_repo.delete(db_apis.get_session(),
                                id=member[constants.MEMBER_ID])

    def revert(self, member, *args, **kwargs):
        """Mark the member ERROR since the delete couldn't happen

        :param member: Member that failed to get deleted
        :returns: None
        """

        LOG.warning("Reverting delete in DB for member id %s",
                    member[constants.MEMBER_ID])
        try:
            self.member_repo.update(db_apis.get_session(),
                                    member[constants.MEMBER_ID],
                                    provisioning_status=constants.ERROR)
        except Exception as e:
            LOG.error("Failed to update member %(mem)s "
                      "provisioning_status to ERROR due to: %(except)s",
                      {'mem': member[constants.MEMBER_ID], 'except': str(e)})


class DeleteListenerInDB(BaseDatabaseTask):
    """Delete the listener in the DB."""

    def execute(self, listener):
        """Delete the listener in DB

        :param listener: The listener to delete
        :returns: None
        """
        LOG.debug("Delete in DB for listener id: %s",
                  listener[constants.LISTENER_ID])
        self.listener_repo.delete(db_apis.get_session(),
                                  id=listener[constants.LISTENER_ID])

    def revert(self, listener, *args, **kwargs):
        """Mark the listener ERROR since the listener didn't delete

        :param listener: Listener that failed to get deleted
        :returns: None
        """

        # TODO(johnsom) Fix this, it doesn't revert anything
        LOG.warning("Reverting mark listener delete in DB for listener id %s",
                    listener[constants.LISTENER_ID])


class DeletePoolInDB(BaseDatabaseTask):
    """Delete the pool in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, pool_id):
        """Delete the pool in DB

        :param pool_id: The pool_id to be deleted
        :returns: None
        """

        LOG.debug("Delete in DB for pool id: %s ", pool_id)
        self.pool_repo.delete(db_apis.get_session(), id=pool_id)

    def revert(self, pool_id, *args, **kwargs):
        """Mark the pool ERROR since the delete couldn't happen

        :param pool_id: pool_id that failed to get deleted
        :returns: None
        """

        LOG.warning("Reverting delete in DB for pool id %s", pool_id)
        try:
            self.pool_repo.update(db_apis.get_session(), pool_id,
                                  provisioning_status=constants.ERROR)
        except Exception as e:
            LOG.error("Failed to update pool %(pool)s "
                      "provisioning_status to ERROR due to: %(except)s",
                      {'pool': pool_id, 'except': str(e)})


class DeleteL7PolicyInDB(BaseDatabaseTask):
    """Delete the L7 policy in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, l7policy):
        """Delete the l7policy in DB

        :param l7policy: The l7policy to be deleted
        :returns: None
        """

        LOG.debug("Delete in DB for l7policy id: %s ",
                  l7policy[constants.L7POLICY_ID])
        self.l7policy_repo.delete(db_apis.get_session(),
                                  id=l7policy[constants.L7POLICY_ID])

    def revert(self, l7policy, *args, **kwargs):
        """Mark the l7policy ERROR since the delete couldn't happen

        :param l7policy: L7 policy that failed to get deleted
        :returns: None
        """

        LOG.warning("Reverting delete in DB for l7policy id %s",
                    l7policy[constants.L7POLICY_ID])
        try:
            self.l7policy_repo.update(db_apis.get_session(),
                                      l7policy[constants.L7POLICY_ID],
                                      provisioning_status=constants.ERROR)
        except Exception as e:
            LOG.error("Failed to update l7policy %(l7policy)s "
                      "provisioning_status to ERROR due to: %(except)s",
                      {'l7policy': l7policy[constants.L7POLICY_ID],
                       'except': str(e)})


class DeleteL7RuleInDB(BaseDatabaseTask):
    """Delete the L7 rule in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, l7rule):
        """Delete the l7rule in DB

        :param l7rule: The l7rule to be deleted
        :returns: None
        """

        LOG.debug("Delete in DB for l7rule id: %s",
                  l7rule[constants.L7RULE_ID])
        self.l7rule_repo.delete(db_apis.get_session(),
                                id=l7rule[constants.L7RULE_ID])

    def revert(self, l7rule, *args, **kwargs):
        """Mark the l7rule ERROR since the delete couldn't happen

        :param l7rule: L7 rule that failed to get deleted
        :returns: None
        """

        LOG.warning("Reverting delete in DB for l7rule id %s",
                    l7rule[constants.L7RULE_ID])
        try:
            self.l7rule_repo.update(db_apis.get_session(),
                                    l7rule[constants.L7RULE_ID],
                                    provisioning_status=constants.ERROR)
        except Exception as e:
            LOG.error("Failed to update l7rule %(l7rule)s "
                      "provisioning_status to ERROR due to: %(except)s",
                      {'l7rule': l7rule[constants.L7RULE_ID],
                       'except': str(e)})


class ReloadAmphora(BaseDatabaseTask):
    """Get an amphora object from the database."""

    def execute(self, amphora):
        """Get an amphora object from the database.

        :param amphora_id: The amphora ID to lookup
        :returns: The amphora object
        """

        LOG.debug("Get amphora from DB for amphora id: %s ",
                  amphora[constants.ID])
        return self.amphora_repo.get(db_apis.get_session(),
                                     id=amphora[constants.ID]).to_dict()


class ReloadLoadBalancer(BaseDatabaseTask):
    """Get an load balancer object from the database."""

    def execute(self, loadbalancer_id, *args, **kwargs):
        """Get an load balancer object from the database.

        :param loadbalancer_id: The load balancer ID to lookup
        :returns: The load balancer object
        """

        LOG.debug("Get load balancer from DB for load balancer id: %s ",
                  loadbalancer_id)
        db_lb = self.loadbalancer_repo.get(db_apis.get_session(),
                                           id=loadbalancer_id)
        lb_dict = provider_utils.db_loadbalancer_to_provider_loadbalancer(
            db_lb)
        return lb_dict.to_dict()


class UpdateVIPAfterAllocation(BaseDatabaseTask):
    """Update a VIP associated with a given load balancer."""

    def execute(self, loadbalancer_id, vip):
        """Update a VIP associated with a given load balancer.

        :param loadbalancer_id: Id of a load balancer which VIP should be
               updated.
        :param vip: data_models.Vip object with update data.
        :returns: The load balancer object.
        """
        self.repos.vip.update(db_apis.get_session(), loadbalancer_id,
                              port_id=vip[constants.PORT_ID],
                              subnet_id=vip[constants.SUBNET_ID],
                              ip_address=vip[constants.IP_ADDRESS])
        db_lb = self.repos.load_balancer.get(db_apis.get_session(),
                                             id=loadbalancer_id)
        return provider_utils.db_loadbalancer_to_provider_loadbalancer(
            db_lb).to_dict()


class UpdateAmphoraeVIPData(BaseDatabaseTask):
    """Update amphorae VIP data."""

    def execute(self, amps_data):
        """Update amphorae VIP data.

        :param amps_data: Amphorae update dicts.
        :returns: None
        """
        for amp_data in amps_data:
            self.repos.amphora.update(
                db_apis.get_session(),
                amp_data.get(constants.ID),
                vrrp_ip=amp_data[constants.VRRP_IP],
                ha_ip=amp_data[constants.HA_IP],
                vrrp_port_id=amp_data[constants.VRRP_PORT_ID],
                ha_port_id=amp_data[constants.HA_PORT_ID],
                vrrp_id=1)


class UpdateAmphoraVIPData(BaseDatabaseTask):
    """Update amphorae VIP data."""

    def execute(self, amp_data):
        """Update amphorae VIP data.

        :param amps_data: Amphorae update dicts.
        :returns: None
        """
        self.repos.amphora.update(
            db_apis.get_session(),
            amp_data.get(constants.ID),
            vrrp_ip=amp_data[constants.VRRP_IP],
            ha_ip=amp_data[constants.HA_IP],
            vrrp_port_id=amp_data[constants.VRRP_PORT_ID],
            ha_port_id=amp_data[constants.HA_PORT_ID],
            vrrp_id=1)


class UpdateAmpFailoverDetails(BaseDatabaseTask):
    """Update amphora failover details in the database."""

    def execute(self, amphora, vip, base_port):
        """Update amphora failover details in the database.

        :param amphora: The amphora to update
        :param vip: The VIP object associated with this amphora.
        :param base_port: The base port object associated with the amphora.
        :returns: None
        """
        # role and vrrp_priority will be updated later.
        self.repos.amphora.update(
            db_apis.get_session(),
            amphora.get(constants.ID),
            # TODO(johnsom) We should do a better job getting the fixed_ip
            #               as this could be a problem with dual stack.
            #               Fix this during the multi-vip patch.
            vrrp_ip=base_port[constants.FIXED_IPS][0][constants.IP_ADDRESS],
            ha_ip=vip[constants.IP_ADDRESS],
            vrrp_port_id=base_port[constants.ID],
            ha_port_id=vip[constants.PORT_ID],
            vrrp_id=1)


class AssociateFailoverAmphoraWithLBID(BaseDatabaseTask):
    """Associate failover amphora with loadbalancer in the database."""

    def execute(self, amphora_id, loadbalancer_id):
        """Associate failover amphora with loadbalancer in the database.

        :param amphora_id: Id of an amphora to update
        :param loadbalancer_id: Id of a load balancer to be associated with
               a given amphora.
        :returns: None
        """
        self.repos.amphora.associate(db_apis.get_session(),
                                     load_balancer_id=loadbalancer_id,
                                     amphora_id=amphora_id)

    def revert(self, amphora_id, *args, **kwargs):
        """Remove amphora-load balancer association.

        :param amphora_id: Id of an amphora that couldn't be associated
               with a load balancer.
        :returns: None
        """
        try:
            self.repos.amphora.update(db_apis.get_session(), amphora_id,
                                      loadbalancer_id=None)
        except Exception as e:
            LOG.error("Failed to update amphora %(amp)s "
                      "load balancer id to None due to: "
                      "%(except)s", {'amp': amphora_id, 'except': str(e)})


class _MarkAmphoraRoleAndPriorityInDB(BaseDatabaseTask):
    """Alter the amphora role and priority in DB."""

    def _execute(self, amphora_id, amp_role, vrrp_priority):
        """Alter the amphora role and priority in DB.

        :param amphora_id: Amphora ID to update.
        :param amp_role: Amphora role to be set.
        :param vrrp_priority: VRRP priority to set.
        :returns: None
        """
        LOG.debug("Mark %(role)s in DB for amphora: %(amp)s",
                  {constants.ROLE: amp_role, 'amp': amphora_id})
        self.amphora_repo.update(db_apis.get_session(), amphora_id,
                                 role=amp_role, vrrp_priority=vrrp_priority)

    def _revert(self, result, amphora_id, *args, **kwargs):
        """Removes role and vrrp_priority association.

        :param result: Result of the association.
        :param amphora_id: Amphora ID which role/vrrp_priority association
                           failed.
        :returns: None
        """

        if isinstance(result, failure.Failure):
            return

        LOG.warning("Reverting amphora role in DB for amp id %(amp)s",
                    {'amp': amphora_id})
        try:
            self.amphora_repo.update(db_apis.get_session(), amphora_id,
                                     role=None, vrrp_priority=None)
        except Exception as e:
            LOG.error("Failed to update amphora %(amp)s "
                      "role and vrrp_priority to None due to: "
                      "%(except)s", {'amp': amphora_id, 'except': str(e)})


class MarkAmphoraMasterInDB(_MarkAmphoraRoleAndPriorityInDB):
    """Alter the amphora role to: MASTER."""

    def execute(self, amphora):
        """Mark amphora as MASTER in db.

        :param amphora: Amphora to update role.
        :returns: None
        """
        amp_role = constants.ROLE_MASTER
        self._execute(amphora[constants.ID], amp_role,
                      constants.ROLE_MASTER_PRIORITY)

    def revert(self, result, amphora, *args, **kwargs):
        """Removes amphora role association.

        :param amphora: Amphora to update role.
        :returns: None
        """
        self._revert(result, amphora[constants.ID], *args, **kwargs)


class MarkAmphoraBackupInDB(_MarkAmphoraRoleAndPriorityInDB):
    """Alter the amphora role to: Backup."""

    def execute(self, amphora):
        """Mark amphora as BACKUP in db.

        :param amphora: Amphora to update role.
        :returns: None
        """
        amp_role = constants.ROLE_BACKUP
        self._execute(amphora[constants.ID], amp_role,
                      constants.ROLE_BACKUP_PRIORITY)

    def revert(self, result, amphora, *args, **kwargs):
        """Removes amphora role association.

        :param amphora: Amphora to update role.
        :returns: None
        """
        self._revert(result, amphora[constants.ID], *args, **kwargs)


class MarkAmphoraStandAloneInDB(_MarkAmphoraRoleAndPriorityInDB):
    """Alter the amphora role to: Standalone."""

    def execute(self, amphora):
        """Mark amphora as STANDALONE in db.

        :param amphora: Amphora to update role.
        :returns: None
        """
        amp_role = constants.ROLE_STANDALONE
        self._execute(amphora[constants.ID], amp_role, None)

    def revert(self, result, amphora, *args, **kwargs):
        """Removes amphora role association.

        :param amphora: Amphora to update role.
        :returns: None
        """
        self._revert(result, amphora[constants.ID], *args, **kwargs)


class MarkAmphoraAllocatedInDB(BaseDatabaseTask):
    """Will mark an amphora as allocated to a load balancer in the database.

    Assume sqlalchemy made sure the DB got
    retried sufficiently - so just abort
    """

    def execute(self, amphora, loadbalancer_id):
        """Mark amphora as allocated to a load balancer in DB.

        :param amphora: Amphora to be updated.
        :param loadbalancer_id: Id of a load balancer to which an amphora
               should be allocated.
        :returns: None
        """

        LOG.info('Mark ALLOCATED in DB for amphora: %(amp)s with '
                 'compute id %(comp)s for load balancer: %(lb)s',
                 {
                     'amp': amphora.get(constants.ID),
                     'comp': amphora[constants.COMPUTE_ID],
                     'lb': loadbalancer_id
                 })
        self.amphora_repo.update(
            db_apis.get_session(),
            amphora.get(constants.ID),
            status=constants.AMPHORA_ALLOCATED,
            compute_id=amphora[constants.COMPUTE_ID],
            lb_network_ip=amphora[constants.LB_NETWORK_IP],
            load_balancer_id=loadbalancer_id)

    def revert(self, result, amphora, loadbalancer_id, *args, **kwargs):
        """Mark the amphora as broken and ready to be cleaned up.

        :param result: Execute task result
        :param amphora: Amphora that was updated.
        :param loadbalancer_id: Id of a load balancer to which an amphora
               failed to be allocated.
        :returns: None
        """

        if isinstance(result, failure.Failure):
            return

        LOG.warning("Reverting mark amphora ready in DB for amp "
                    "id %(amp)s and compute id %(comp)s",
                    {'amp': amphora.get(constants.ID),
                     'comp': amphora[constants.COMPUTE_ID]})
        self.task_utils.mark_amphora_status_error(
            amphora.get(constants.ID))


class MarkAmphoraBootingInDB(BaseDatabaseTask):
    """Mark the amphora as booting in the database."""

    def execute(self, amphora_id, compute_id):
        """Mark amphora booting in DB.

        :param amphora_id: Id of the amphora to update
        :param compute_id: Id of a compute on which an amphora resides
        :returns: None
        """

        LOG.debug("Mark BOOTING in DB for amphora: %(amp)s with "
                  "compute id %(id)s", {'amp': amphora_id,
                                        constants.ID: compute_id})
        self.amphora_repo.update(db_apis.get_session(), amphora_id,
                                 status=constants.AMPHORA_BOOTING,
                                 compute_id=compute_id)

    def revert(self, result, amphora_id, compute_id, *args, **kwargs):
        """Mark the amphora as broken and ready to be cleaned up.

        :param result: Execute task result
        :param amphora_id: Id of the amphora that failed to update
        :param compute_id: Id of a compute on which an amphora resides
        :returns: None
        """

        if isinstance(result, failure.Failure):
            return

        LOG.warning("Reverting mark amphora booting in DB for amp "
                    "id %(amp)s and compute id %(comp)s",
                    {'amp': amphora_id, 'comp': compute_id})
        try:
            self.amphora_repo.update(db_apis.get_session(), amphora_id,
                                     status=constants.ERROR,
                                     compute_id=compute_id)
        except Exception as e:
            LOG.error("Failed to update amphora %(amp)s "
                      "status to ERROR due to: "
                      "%(except)s", {'amp': amphora_id, 'except': str(e)})


class MarkAmphoraDeletedInDB(BaseDatabaseTask):
    """Mark the amphora deleted in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, amphora):
        """Mark the amphora as deleted in DB.

        :param amphora: Amphora to be updated.
        :returns: None
        """

        LOG.debug("Mark DELETED in DB for amphora: %(amp)s with "
                  "compute id %(comp)s",
                  {'amp': amphora[constants.ID],
                   'comp': amphora[constants.COMPUTE_ID]})
        self.amphora_repo.update(db_apis.get_session(),
                                 amphora[constants.ID],
                                 status=constants.DELETED)

    def revert(self, amphora, *args, **kwargs):
        """Mark the amphora as broken and ready to be cleaned up.

        :param amphora: Amphora that was updated.
        :returns: None
        """

        LOG.warning("Reverting mark amphora deleted in DB "
                    "for amp id %(amp)s and compute id %(comp)s",
                    {'amp': amphora[constants.ID],
                     'comp': amphora[constants.COMPUTE_ID]})

        self.task_utils.mark_amphora_status_error(amphora[constants.ID])


class MarkAmphoraPendingDeleteInDB(BaseDatabaseTask):
    """Mark the amphora pending delete in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, amphora):
        """Mark the amphora as pending delete in DB.

        :param amphora: Amphora to be updated.
        :returns: None
        """

        LOG.debug("Mark PENDING DELETE in DB for amphora: %(amp)s "
                  "with compute id %(id)s",
                  {'amp': amphora[constants.ID],
                   'id': amphora[constants.COMPUTE_ID]})
        self.amphora_repo.update(db_apis.get_session(),
                                 amphora[constants.ID],
                                 status=constants.PENDING_DELETE)

    def revert(self, amphora, *args, **kwargs):
        """Mark the amphora as broken and ready to be cleaned up.

        :param amphora: Amphora that was updated.
        :returns: None
        """

        LOG.warning("Reverting mark amphora pending delete in DB "
                    "for amp id %(amp)s and compute id %(comp)s",
                    {'amp': amphora[constants.ID],
                     'comp': amphora[constants.COMPUTE_ID]})
        self.task_utils.mark_amphora_status_error(amphora[constants.ID])


class MarkAmphoraPendingUpdateInDB(BaseDatabaseTask):
    """Mark the amphora pending update in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, amphora):
        """Mark the amphora as pending update in DB.

        :param amphora: Amphora to be updated.
        :returns: None
        """

        LOG.debug("Mark PENDING UPDATE in DB for amphora: %(amp)s "
                  "with compute id %(id)s",
                  {'amp': amphora.get(constants.ID),
                   'id': amphora[constants.COMPUTE_ID]})
        self.amphora_repo.update(db_apis.get_session(),
                                 amphora.get(constants.ID),
                                 status=constants.PENDING_UPDATE)

    def revert(self, amphora, *args, **kwargs):
        """Mark the amphora as broken and ready to be cleaned up.

        :param amphora: Amphora that was updated.
        :returns: None
        """

        LOG.warning("Reverting mark amphora pending update in DB "
                    "for amp id %(amp)s and compute id %(comp)s",
                    {'amp': amphora.get(constants.ID),
                     'comp': amphora[constants.COMPUTE_ID]})
        self.task_utils.mark_amphora_status_error(amphora.get(constants.ID))


class MarkAmphoraReadyInDB(BaseDatabaseTask):
    """This task will mark an amphora as ready in the database.

    Assume sqlalchemy made sure the DB got
    retried sufficiently - so just abort
    """

    def execute(self, amphora):
        """Mark amphora as ready in DB.

        :param amphora: Amphora to be updated.
        :returns: None
        """

        LOG.info("Mark READY in DB for amphora: %(amp)s with compute "
                 "id %(comp)s",
                 {"amp": amphora.get(constants.ID),
                  "comp": amphora[constants.COMPUTE_ID]})
        self.amphora_repo.update(
            db_apis.get_session(),
            amphora.get(constants.ID),
            status=constants.AMPHORA_READY,
            compute_id=amphora[constants.COMPUTE_ID],
            lb_network_ip=amphora[constants.LB_NETWORK_IP])

    def revert(self, amphora, *args, **kwargs):
        """Mark the amphora as broken and ready to be cleaned up.

        :param amphora: Amphora that was updated.
        :returns: None
        """

        LOG.warning("Reverting mark amphora ready in DB for amp "
                    "id %(amp)s and compute id %(comp)s",
                    {'amp': amphora.get(constants.ID),
                     'comp': amphora[constants.COMPUTE_ID]})
        try:
            self.amphora_repo.update(
                db_apis.get_session(),
                amphora.get(constants.ID),
                status=constants.ERROR,
                compute_id=amphora[constants.COMPUTE_ID],
                lb_network_ip=amphora[constants.LB_NETWORK_IP])
        except Exception as e:
            LOG.error("Failed to update amphora %(amp)s "
                      "status to ERROR due to: "
                      "%(except)s", {'amp': amphora.get(constants.ID),
                                     'except': str(e)})


class UpdateAmphoraComputeId(BaseDatabaseTask):
    """Associate amphora with a compute in DB."""

    def execute(self, amphora_id, compute_id):
        """Associate amphora with a compute in DB.

        :param amphora_id: Id of the amphora to update
        :param compute_id: Id of a compute on which an amphora resides
        :returns: None
        """

        self.amphora_repo.update(db_apis.get_session(), amphora_id,
                                 compute_id=compute_id)


class UpdateAmphoraInfo(BaseDatabaseTask):
    """Update amphora with compute instance details."""

    def execute(self, amphora_id, compute_obj):
        """Update amphora with compute instance details.

        :param amphora_id: Id of the amphora to update
        :param compute_obj: Compute on which an amphora resides
        :returns: Updated amphora object
        """
        self.amphora_repo.update(
            db_apis.get_session(), amphora_id,
            lb_network_ip=compute_obj[constants.LB_NETWORK_IP],
            cached_zone=compute_obj[constants.CACHED_ZONE],
            image_id=compute_obj[constants.IMAGE_ID],
            compute_flavor=compute_obj[constants.COMPUTE_FLAVOR])
        return self.amphora_repo.get(db_apis.get_session(),
                                     id=amphora_id).to_dict()


class UpdateAmphoraDBCertExpiration(BaseDatabaseTask):
    """Update the amphora expiration date with new cert file date."""

    def execute(self, amphora_id, server_pem):
        """Update the amphora expiration date with new cert file date.

        :param amphora_id: Id of the amphora to update
        :param server_pem: Certificate in PEM format
        :returns: None
        """

        LOG.debug("Update DB cert expiry date of amphora id: %s", amphora_id)

        key = utils.get_compatible_server_certs_key_passphrase()
        fer = fernet.Fernet(key)
        cert_expiration = cert_parser.get_cert_expiration(
            fer.decrypt(server_pem.encode("utf-8")))
        LOG.debug("Certificate expiration date is %s ", cert_expiration)
        self.amphora_repo.update(db_apis.get_session(), amphora_id,
                                 cert_expiration=cert_expiration)


class UpdateAmphoraCertBusyToFalse(BaseDatabaseTask):
    """Update the amphora cert_busy flag to be false."""

    def execute(self, amphora_id):
        """Update the amphora cert_busy flag to be false.

        :param amphora: Amphora to be updated.
        :returns: None
        """

        LOG.debug("Update cert_busy flag of amphora id %s to False",
                  amphora_id)
        self.amphora_repo.update(db_apis.get_session(), amphora_id,
                                 cert_busy=False)


class MarkLBActiveInDB(BaseDatabaseTask):
    """Mark the load balancer active in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def __init__(self, mark_subobjects=False, **kwargs):
        super().__init__(**kwargs)
        self.mark_subobjects = mark_subobjects

    def execute(self, loadbalancer):
        """Mark the load balancer as active in DB.

        This also marks ACTIVE all sub-objects of the load balancer if
        self.mark_subobjects is True.

        :param loadbalancer: Load balancer object to be updated
        :returns: None
        """

        if self.mark_subobjects:
            LOG.debug("Marking all listeners of loadbalancer %s ACTIVE",
                      loadbalancer[constants.LOADBALANCER_ID])
            db_lb = self.loadbalancer_repo.get(
                db_apis.get_session(),
                id=loadbalancer[constants.LOADBALANCER_ID])
            for listener in db_lb.listeners:
                self._mark_listener_status(listener, constants.ACTIVE)

        LOG.info("Mark ACTIVE in DB for load balancer id: %s",
                 loadbalancer[constants.LOADBALANCER_ID])
        self.loadbalancer_repo.update(db_apis.get_session(),
                                      loadbalancer[constants.LOADBALANCER_ID],
                                      provisioning_status=constants.ACTIVE)

    def _mark_listener_status(self, listener, status):
        self.listener_repo.update(db_apis.get_session(),
                                  listener.id,
                                  provisioning_status=status)
        LOG.debug("Marking all l7policies of listener %s %s",
                  listener.id, status)
        for l7policy in listener.l7policies:
            self._mark_l7policy_status(l7policy, status)

        if listener.default_pool:
            LOG.debug("Marking default pool of listener %s %s",
                      listener.id, status)
            self._mark_pool_status(listener.default_pool, status)

    def _mark_l7policy_status(self, l7policy, status):
        self.l7policy_repo.update(
            db_apis.get_session(), l7policy.id,
            provisioning_status=status)

        LOG.debug("Marking all l7rules of l7policy %s %s",
                  l7policy.id, status)
        for l7rule in l7policy.l7rules:
            self._mark_l7rule_status(l7rule, status)

        if l7policy.redirect_pool:
            LOG.debug("Marking redirect pool of l7policy %s %s",
                      l7policy.id, status)
            self._mark_pool_status(l7policy.redirect_pool, status)

    def _mark_l7rule_status(self, l7rule, status):
        self.l7rule_repo.update(
            db_apis.get_session(), l7rule.id,
            provisioning_status=status)

    def _mark_pool_status(self, pool, status):
        self.pool_repo.update(
            db_apis.get_session(), pool.id,
            provisioning_status=status)
        if pool.health_monitor:
            LOG.debug("Marking health monitor of pool %s %s", pool.id, status)
            self._mark_hm_status(pool.health_monitor, status)

        LOG.debug("Marking all members of pool %s %s", pool.id, status)
        for member in pool.members:
            self._mark_member_status(member, status)

    def _mark_hm_status(self, hm, status):
        self.health_mon_repo.update(
            db_apis.get_session(), hm.id,
            provisioning_status=status)

    def _mark_member_status(self, member, status):
        self.member_repo.update(
            db_apis.get_session(), member.id,
            provisioning_status=status)

    def revert(self, loadbalancer, *args, **kwargs):
        """Mark the load balancer as broken and ready to be cleaned up.

        This also puts all sub-objects of the load balancer to ERROR state if
        self.mark_subobjects is True

        :param loadbalancer: Load balancer object that failed to update
        :returns: None
        """

        if self.mark_subobjects:
            LOG.debug("Marking all listeners of loadbalancer %s ERROR",
                      loadbalancer[constants.LOADBALANCER_ID])
            db_lb = self.loadbalancer_repo.get(
                db_apis.get_session(),
                id=loadbalancer[constants.LOADBALANCER_ID])
            for listener in db_lb.listeners:
                try:
                    self._mark_listener_status(listener, constants.ERROR)
                except Exception:
                    LOG.warning("Error updating listener %s provisioning "
                                "status", listener.id)

        LOG.warning("Reverting mark load balancer deleted in DB "
                    "for load balancer id %s",
                    loadbalancer[constants.LOADBALANCER_ID])
        self.task_utils.mark_loadbalancer_prov_status_error(
            loadbalancer[constants.LOADBALANCER_ID])


class MarkLBActiveInDBByListener(BaseDatabaseTask):
    """Mark the load balancer active in the DB using a listener dict.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, listener):
        """Mark the load balancer as active in DB.

        :param listener: Listener dictionary
        :returns: None
        """

        LOG.info("Mark ACTIVE in DB for load balancer id: %s",
                 listener[constants.LOADBALANCER_ID])
        self.loadbalancer_repo.update(db_apis.get_session(),
                                      listener[constants.LOADBALANCER_ID],
                                      provisioning_status=constants.ACTIVE)

    def revert(self, listener, *args, **kwargs):
        """Mark the load balancer as broken and ready to be cleaned up.

        This also puts all sub-objects of the load balancer to ERROR state if
        self.mark_subobjects is True

        :param listener: Listener dictionary
        :returns: None
        """

        LOG.warning("Reverting mark load balancer active in DB "
                    "for load balancer id %s",
                    listener[constants.LOADBALANCER_ID])
        self.task_utils.mark_loadbalancer_prov_status_error(
            listener[constants.LOADBALANCER_ID])


class UpdateLBServerGroupInDB(BaseDatabaseTask):
    """Update the server group id info for load balancer in DB."""

    def execute(self, loadbalancer_id, server_group_id):
        """Update the server group id info for load balancer in DB.

        :param loadbalancer_id: Id of a load balancer to update
        :param server_group_id: Id of a server group to associate with
               the load balancer
        :returns: None
        """

        LOG.debug("Server Group updated with id: %s for load balancer id: %s:",
                  server_group_id, loadbalancer_id)
        self.loadbalancer_repo.update(db_apis.get_session(),
                                      id=loadbalancer_id,
                                      server_group_id=server_group_id)

    def revert(self, loadbalancer_id, server_group_id, *args, **kwargs):
        """Remove server group information from a load balancer in DB.

        :param loadbalancer_id: Id of a load balancer that failed to update
        :param server_group_id: Id of a server group that couldn't be
               associated with the load balancer
        :returns: None
        """
        LOG.warning('Reverting Server Group updated with id: %(s1)s for '
                    'load balancer id: %(s2)s ',
                    {'s1': server_group_id, 's2': loadbalancer_id})
        try:
            self.loadbalancer_repo.update(db_apis.get_session(),
                                          id=loadbalancer_id,
                                          server_group_id=None)
        except Exception as e:
            LOG.error("Failed to update load balancer %(lb)s "
                      "server_group_id to None due to: "
                      "%(except)s", {'lb': loadbalancer_id, 'except': str(e)})


class MarkLBDeletedInDB(BaseDatabaseTask):
    """Mark the load balancer deleted in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, loadbalancer):
        """Mark the load balancer as deleted in DB.

        :param loadbalancer: Load balancer object to be updated
        :returns: None
        """

        LOG.debug("Mark DELETED in DB for load balancer id: %s",
                  loadbalancer[constants.LOADBALANCER_ID])
        self.loadbalancer_repo.update(db_apis.get_session(),
                                      loadbalancer[constants.LOADBALANCER_ID],
                                      provisioning_status=constants.DELETED)

    def revert(self, loadbalancer, *args, **kwargs):
        """Mark the load balancer as broken and ready to be cleaned up.

        :param loadbalancer: Load balancer object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark load balancer deleted in DB "
                    "for load balancer id %s",
                    loadbalancer[constants.LOADBALANCER_ID])
        self.task_utils.mark_loadbalancer_prov_status_error(
            loadbalancer[constants.LOADBALANCER_ID])


class MarkLBPendingDeleteInDB(BaseDatabaseTask):
    """Mark the load balancer pending delete in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, loadbalancer):
        """Mark the load balancer as pending delete in DB.

        :param loadbalancer: Load balancer object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING DELETE in DB for load balancer id: %s",
                  loadbalancer[constants.LOADBALANCER_ID])
        self.loadbalancer_repo.update(db_apis.get_session(),
                                      loadbalancer[constants.LOADBALANCER_ID],
                                      provisioning_status=(constants.
                                                           PENDING_DELETE))

    def revert(self, loadbalancer, *args, **kwargs):
        """Mark the load balancer as broken and ready to be cleaned up.

        :param loadbalancer: Load balancer object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark load balancer pending delete in DB "
                    "for load balancer id %s",
                    loadbalancer[constants.LOADBALANCER_ID])
        self.task_utils.mark_loadbalancer_prov_status_error(
            loadbalancer[constants.LOADBALANCER_ID])


class MarkLBAndListenersActiveInDB(BaseDatabaseTask):
    """Mark the load balancer and specified listeners active in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, loadbalancer_id, listeners):
        """Mark the load balancer and listeners as active in DB.

        :param loadbalancer_id: The load balancer ID to be updated
        :param listeners: Listener objects to be updated
        :returns: None
        """
        lb_id = None
        if loadbalancer_id:
            lb_id = loadbalancer_id
        elif listeners:
            lb_id = listeners[0][constants.LOADBALANCER_ID]

        if lb_id:
            LOG.debug("Mark ACTIVE in DB for load balancer id: %s "
                      "and updating status for listener ids: %s", lb_id,
                      ', '.join([listener[constants.LISTENER_ID]
                                for listener in listeners]))
            self.loadbalancer_repo.update(db_apis.get_session(), lb_id,
                                          provisioning_status=constants.ACTIVE)
        for listener in listeners:
            self.listener_repo.prov_status_active_if_not_error(
                db_apis.get_session(), listener[constants.LISTENER_ID])

    def revert(self, loadbalancer_id, listeners, *args, **kwargs):
        """Mark the load balancer and listeners as broken.

        :param loadbalancer_id: The load balancer ID to be updated
        :param listeners: Listener objects that failed to update
        :returns: None
        """
        lb_id = None
        if loadbalancer_id:
            lb_id = loadbalancer_id
        elif listeners:
            lb_id = listeners[0][constants.LOADBALANCER_ID]

        if lb_id:
            lists = ', '.join([listener[constants.LISTENER_ID]
                              for listener in listeners])
            LOG.warning("Reverting mark load balancer and listeners active in "
                        "DB for load balancer id %(LB)s and listener ids: "
                        "%(list)s", {'LB': lb_id,
                                     'list': lists})
            self.task_utils.mark_loadbalancer_prov_status_error(lb_id)

        for listener in listeners:
            self.task_utils.mark_listener_prov_status_error(
                listener[constants.LISTENER_ID])


class MarkListenerDeletedInDB(BaseDatabaseTask):
    """Mark the listener deleted in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, listener):
        """Mark the listener as deleted in DB

        :param listener: The listener to be marked deleted
        :returns: None
        """

        LOG.debug("Mark DELETED in DB for listener id: %s ", listener.id)
        self.listener_repo.update(db_apis.get_session(), listener.id,
                                  provisioning_status=constants.DELETED)

    def revert(self, listener, *args, **kwargs):
        """Mark the listener ERROR since the delete couldn't happen

        :param listener: The listener that couldn't be updated
        :returns: None
        """

        LOG.warning("Reverting mark listener deleted in DB "
                    "for listener id %s", listener.id)
        self.task_utils.mark_listener_prov_status_error(listener.id)


class MarkListenerPendingDeleteInDB(BaseDatabaseTask):
    """Mark the listener pending delete in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, listener):
        """Mark the listener as pending delete in DB.

        :param listener: The listener to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING DELETE in DB for listener id: %s",
                  listener.id)
        self.listener_repo.update(db_apis.get_session(), listener.id,
                                  provisioning_status=constants.PENDING_DELETE)

    def revert(self, listener, *args, **kwargs):
        """Mark the listener as broken and ready to be cleaned up.

        :param listener: The listener that couldn't be updated
        :returns: None
        """

        LOG.warning("Reverting mark listener pending delete in DB "
                    "for listener id %s", listener.id)
        self.task_utils.mark_listener_prov_status_error(listener.id)


class UpdateLoadbalancerInDB(BaseDatabaseTask):
    """Update the loadbalancer in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, loadbalancer, update_dict):
        """Update the loadbalancer in the DB

        :param loadbalancer: The load balancer to be updated
        :param update_dict: The dictionary of updates to apply
        :returns: None
        """

        LOG.debug("Update DB for loadbalancer id: %s ",
                  loadbalancer[constants.LOADBALANCER_ID])
        if update_dict.get('vip'):
            vip_dict = update_dict.pop('vip')
            self.vip_repo.update(db_apis.get_session(),
                                 loadbalancer[constants.LOADBALANCER_ID],
                                 **vip_dict)
        self.loadbalancer_repo.update(db_apis.get_session(),
                                      loadbalancer[constants.LOADBALANCER_ID],
                                      **update_dict)

    def revert(self, loadbalancer, *args, **kwargs):
        """Mark the loadbalancer ERROR since the update couldn't happen

        :param loadbalancer: The load balancer that couldn't be updated
        :returns: None
        """

        LOG.warning("Reverting update loadbalancer in DB "
                    "for loadbalancer id %s",
                    loadbalancer[constants.LOADBALANCER_ID])

        self.task_utils.mark_loadbalancer_prov_status_error(
            loadbalancer[constants.LOADBALANCER_ID])


class UpdateHealthMonInDB(BaseDatabaseTask):
    """Update the health monitor in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, health_mon, update_dict):
        """Update the health monitor in the DB

        :param health_mon: The health monitor to be updated
        :param update_dict: The dictionary of updates to apply
        :returns: None
        """

        LOG.debug("Update DB for health monitor id: %s ",
                  health_mon[constants.HEALTHMONITOR_ID])
        self.health_mon_repo.update(db_apis.get_session(),
                                    health_mon[constants.HEALTHMONITOR_ID],
                                    **update_dict)

    def revert(self, health_mon, *args, **kwargs):
        """Mark the health monitor ERROR since the update couldn't happen

        :param health_mon: The health monitor that couldn't be updated
        :returns: None
        """

        LOG.warning("Reverting update health monitor in DB "
                    "for health monitor id %s",
                    health_mon[constants.HEALTHMONITOR_ID])
        try:
            self.health_mon_repo.update(
                db_apis.get_session(),
                health_mon[constants.HEALTHMONITOR_ID],
                provisioning_status=constants.ERROR)
        except Exception as e:
            LOG.error("Failed to update health monitor %(hm)s "
                      "provisioning_status to ERROR due to: %(except)s",
                      {'hm': health_mon[constants.HEALTHMONITOR_ID],
                       'except': str(e)})


class UpdateListenerInDB(BaseDatabaseTask):
    """Update the listener in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, listener, update_dict):
        """Update the listener in the DB

        :param listener: The listener to be updated
        :param update_dict: The dictionary of updates to apply
        :returns: None
        """

        LOG.debug("Update DB for listener id: %s ",
                  listener[constants.LISTENER_ID])
        self.listener_repo.update(db_apis.get_session(),
                                  listener[constants.LISTENER_ID],
                                  **update_dict)

    def revert(self, listener, *args, **kwargs):
        """Mark the listener ERROR since the update couldn't happen

        :param listener: The listener that couldn't be updated
        :returns: None
        """

        LOG.warning("Reverting update listener in DB "
                    "for listener id %s", listener[constants.LISTENER_ID])
        self.task_utils.mark_listener_prov_status_error(
            listener[constants.LISTENER_ID])


class UpdateMemberInDB(BaseDatabaseTask):
    """Update the member in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, member, update_dict):
        """Update the member in the DB

        :param member: The member to be updated
        :param update_dict: The dictionary of updates to apply
        :returns: None
        """

        LOG.debug("Update DB for member id: %s ", member[constants.MEMBER_ID])
        self.member_repo.update(db_apis.get_session(),
                                member[constants.MEMBER_ID],
                                **update_dict)

    def revert(self, member, *args, **kwargs):
        """Mark the member ERROR since the update couldn't happen

        :param member: The member that couldn't be updated
        :returns: None
        """

        LOG.warning("Reverting update member in DB "
                    "for member id %s", member[constants.MEMBER_ID])
        try:
            self.member_repo.update(db_apis.get_session(),
                                    member[constants.MEMBER_ID],
                                    provisioning_status=constants.ERROR)
        except Exception as e:
            LOG.error("Failed to update member %(member)s provisioning_status "
                      "to ERROR due to: %(except)s",
                      {'member': member[constants.MEMBER_ID],
                       'except': str(e)})


class UpdatePoolInDB(BaseDatabaseTask):
    """Update the pool in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, pool_id, update_dict):
        """Update the pool in the DB

        :param pool_id: The pool_id to be updated
        :param update_dict: The dictionary of updates to apply
        :returns: None
        """

        LOG.debug("Update DB for pool id: %s ", pool_id)
        self.repos.update_pool_and_sp(db_apis.get_session(), pool_id,
                                      update_dict)

    def revert(self, pool_id, *args, **kwargs):
        """Mark the pool ERROR since the update couldn't happen

        :param pool_id: The pool_id that couldn't be updated
        :returns: None
        """

        LOG.warning("Reverting update pool in DB for pool id %s", pool_id)
        try:
            self.repos.update_pool_and_sp(
                db_apis.get_session(), pool_id,
                dict(provisioning_status=constants.ERROR))
        except Exception as e:
            LOG.error("Failed to update pool %(pool)s provisioning_status to "
                      "ERROR due to: %(except)s", {'pool': pool_id,
                                                   'except': str(e)})


class UpdateL7PolicyInDB(BaseDatabaseTask):
    """Update the L7 policy in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, l7policy, update_dict):
        """Update the L7 policy in the DB

        :param l7policy: The L7 policy to be updated
        :param update_dict: The dictionary of updates to apply
        :returns: None
        """

        LOG.debug("Update DB for l7policy id: %s",
                  l7policy[constants.L7POLICY_ID])
        self.l7policy_repo.update(db_apis.get_session(),
                                  l7policy[constants.L7POLICY_ID],
                                  **update_dict)

    def revert(self, l7policy, *args, **kwargs):
        """Mark the l7policy ERROR since the update couldn't happen

        :param l7policy: L7 policy that couldn't be updated
        :returns: None
        """

        LOG.warning("Reverting update l7policy in DB "
                    "for l7policy id %s", l7policy[constants.L7POLICY_ID])
        try:
            self.l7policy_repo.update(db_apis.get_session(),
                                      l7policy[constants.L7POLICY_ID],
                                      provisioning_status=constants.ERROR)
        except Exception as e:
            LOG.error("Failed to update l7policy %(l7p)s provisioning_status "
                      "to ERROR due to: %(except)s",
                      {'l7p': l7policy[constants.L7POLICY_ID],
                       'except': str(e)})


class UpdateL7RuleInDB(BaseDatabaseTask):
    """Update the L7 rule in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, l7rule, update_dict):
        """Update the L7 rule in the DB

        :param l7rule: The L7 rule to be updated
        :param update_dict: The dictionary of updates to apply
        :returns: None
        """

        LOG.debug("Update DB for l7rule id: %s", l7rule[constants.L7RULE_ID])
        self.l7rule_repo.update(db_apis.get_session(),
                                l7rule[constants.L7RULE_ID],
                                **update_dict)

    def revert(self, l7rule, *args, **kwargs):
        """Mark the L7 rule ERROR since the update couldn't happen

        :param l7rule: L7 rule that couldn't be updated
        :returns: None
        """

        LOG.warning("Reverting update l7rule in DB "
                    "for l7rule id %s", l7rule[constants.L7RULE_ID])
        try:
            self.l7policy_repo.update(db_apis.get_session(),
                                      l7rule[constants.L7POLICY_ID],
                                      provisioning_status=constants.ERROR)
        except Exception as e:
            LOG.error("Failed to update L7rule %(l7r)s provisioning_status to "
                      "ERROR due to: %(except)s",
                      {'l7r': l7rule[constants.L7POLICY_ID], 'except': str(e)})


class GetAmphoraDetails(BaseDatabaseTask):
    """Task to retrieve amphora network details."""

    def execute(self, amphora):
        """Retrieve amphora network details.

        :param amphora: Amphora which network details are required
        :returns: Amphora data dict
        """
        db_amp = self.amphora_repo.get(db_apis.get_session(),
                                       id=amphora.get(constants.ID))
        amphora.update({
            constants.VRRP_IP: db_amp.vrrp_ip,
            constants.HA_IP: db_amp.ha_ip,
            constants.HA_PORT_ID: db_amp.ha_port_id,
            constants.ROLE: db_amp.role,
            constants.VRRP_ID: db_amp.vrrp_id,
            constants.VRRP_PRIORITY: db_amp.vrrp_priority
        })
        return amphora


class GetAmphoraeFromLoadbalancer(BaseDatabaseTask):
    """Task to pull the amphorae from a loadbalancer."""

    def execute(self, loadbalancer_id):
        """Pull the amphorae from a loadbalancer.

        :param loadbalancer_id: Load balancer ID to get amphorae from
        :returns: A list of Listener objects
        """
        amphorae = []
        db_lb = self.repos.load_balancer.get(db_apis.get_session(),
                                             id=loadbalancer_id)
        for amp in db_lb.amphorae:
            a = self.amphora_repo.get(db_apis.get_session(), id=amp.id,
                                      show_deleted=False)
            if a is None:
                continue
            amphorae.append(a.to_dict())
        return amphorae


class GetListenersFromLoadbalancer(BaseDatabaseTask):
    """Task to pull the listeners from a loadbalancer."""

    def execute(self, loadbalancer):
        """Pull the listeners from a loadbalancer.

        :param loadbalancer: Load balancer which listeners are required
        :returns: A list of Listener objects
        """
        listeners = []
        db_lb = self.repos.load_balancer.get(
            db_apis.get_session(), id=loadbalancer[constants.LOADBALANCER_ID])
        for listener in db_lb.listeners:
            db_l = self.listener_repo.get(db_apis.get_session(),
                                          id=listener.id)
            prov_listener = provider_utils.db_listener_to_provider_listener(
                db_l)
            listeners.append(prov_listener.to_dict())
        return listeners


class GetVipFromLoadbalancer(BaseDatabaseTask):
    """Task to pull the vip from a loadbalancer."""

    def execute(self, loadbalancer):
        """Pull the vip from a loadbalancer.

        :param loadbalancer: Load balancer which VIP is required
        :returns: VIP associated with a given load balancer
        """
        db_lb = self.repos.load_balancer.get(
            db_apis.get_session(), id=loadbalancer[constants.LOADBALANCER_ID])
        return db_lb.vip.to_dict(recurse=True)


class GetLoadBalancer(BaseDatabaseTask):
    """Get an load balancer object from the database."""

    def execute(self, loadbalancer_id, *args, **kwargs):
        """Get an load balancer object from the database.

        :param loadbalancer_id: The load balancer ID to lookup
        :returns: The load balancer object
        """

        LOG.debug("Get load balancer from DB for load balancer id: %s",
                  loadbalancer_id)
        db_lb = self.loadbalancer_repo.get(db_apis.get_session(),
                                           id=loadbalancer_id)
        provider_lb = provider_utils.db_loadbalancer_to_provider_loadbalancer(
            db_lb)
        return provider_lb.to_dict()


class CreateVRRPGroupForLB(BaseDatabaseTask):
    """Create a VRRP group for a load balancer."""

    def execute(self, loadbalancer_id):
        """Create a VRRP group for a load balancer.

        :param loadbalancer_id: Load balancer ID for which a VRRP group
               should be created
        """
        try:
            self.repos.vrrpgroup.create(
                db_apis.get_session(),
                load_balancer_id=loadbalancer_id,
                vrrp_group_name=str(loadbalancer_id).replace('-', ''),
                vrrp_auth_type=constants.VRRP_AUTH_DEFAULT,
                vrrp_auth_pass=uuidutils.generate_uuid().replace('-', '')[0:7],
                advert_int=CONF.keepalived_vrrp.vrrp_advert_int)
        except odb_exceptions.DBDuplicateEntry:
            LOG.debug('VRRP_GROUP entry already exists for load balancer, '
                      'skipping create.')


class DisableAmphoraHealthMonitoring(BaseDatabaseTask):
    """Disable amphora health monitoring.

    This disables amphora health monitoring by removing it from
    the amphora_health table.
    """

    def execute(self, amphora):
        """Disable health monitoring for an amphora

        :param amphora: The amphora to disable health monitoring for
        :returns: None
        """
        self._delete_from_amp_health(amphora[constants.ID])


class DisableLBAmphoraeHealthMonitoring(BaseDatabaseTask):
    """Disable health monitoring on the LB amphorae.

    This disables amphora health monitoring by removing it from
    the amphora_health table for each amphora on a load balancer.
    """

    def execute(self, loadbalancer):
        """Disable health monitoring for amphora on a load balancer

        :param loadbalancer: The load balancer to disable health monitoring on
        :returns: None
        """
        db_lb = self.loadbalancer_repo.get(
            db_apis.get_session(), id=loadbalancer[constants.LOADBALANCER_ID])
        for amphora in db_lb.amphorae:
            self._delete_from_amp_health(amphora.id)


class MarkAmphoraHealthBusy(BaseDatabaseTask):
    """Mark amphora health monitoring busy.

    This prevents amphora failover by marking the amphora busy in
    the amphora_health table.
    """

    def execute(self, amphora):
        """Mark amphora health monitoring busy

        :param amphora: The amphora to mark amphora health busy
        :returns: None
        """
        self._mark_amp_health_busy(amphora[constants.ID])


class MarkLBAmphoraeHealthBusy(BaseDatabaseTask):
    """Mark amphorae health monitoring busy for the LB.

    This prevents amphorae failover by marking each amphora of a given
    load balancer busy in the amphora_health table.
    """

    def execute(self, loadbalancer):
        """Marks amphorae health busy for each amphora on a load balancer

        :param loadbalancer: The load balancer to mark amphorae health busy
        :returns: None
        """
        db_lb = self.loadbalancer_repo.get(
            db_apis.get_session(), id=loadbalancer[constants.LOADBALANCER_ID])
        for amphora in db_lb.amphorae:
            self._mark_amp_health_busy(amphora.id)


class MarkHealthMonitorActiveInDB(BaseDatabaseTask):
    """Mark the health monitor ACTIVE in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, health_mon):
        """Mark the health monitor ACTIVE in DB.

        :param health_mon: Health Monitor object to be updated
        :returns: None
        """

        LOG.debug("Mark ACTIVE in DB for health monitor id: %s",
                  health_mon[constants.HEALTHMONITOR_ID])
        db_health_mon = self.health_mon_repo.get(
            db_apis.get_session(), id=health_mon[constants.HEALTHMONITOR_ID])
        op_status = (constants.ONLINE if db_health_mon.enabled
                     else constants.OFFLINE)
        self.health_mon_repo.update(db_apis.get_session(),
                                    health_mon[constants.HEALTHMONITOR_ID],
                                    provisioning_status=constants.ACTIVE,
                                    operating_status=op_status)

    def revert(self, health_mon, *args, **kwargs):
        """Mark the health monitor as broken

        :param health_mon: Health Monitor object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark health montor ACTIVE in DB "
                    "for health monitor id %s",
                    health_mon[constants.HEALTHMONITOR_ID])
        self.task_utils.mark_health_mon_prov_status_error(
            health_mon[constants.HEALTHMONITOR_ID])


class MarkHealthMonitorPendingCreateInDB(BaseDatabaseTask):
    """Mark the health monitor pending create in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, health_mon):
        """Mark the health monitor as pending create in DB.

        :param health_mon: Health Monitor object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING CREATE in DB for health monitor id: %s",
                  health_mon[constants.HEALTHMONITOR_ID])
        self.health_mon_repo.update(db_apis.get_session(),
                                    health_mon[constants.HEALTHMONITOR_ID],
                                    provisioning_status=(constants.
                                                         PENDING_CREATE))

    def revert(self, health_mon, *args, **kwargs):
        """Mark the health monitor as broken

        :param health_mon: Health Monitor object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark health monitor pending create in DB "
                    "for health monitor id %s",
                    health_mon[constants.HEALTHMONITOR_ID])
        self.task_utils.mark_health_mon_prov_status_error(
            health_mon[constants.HEALTHMONITOR_ID])


class MarkHealthMonitorPendingDeleteInDB(BaseDatabaseTask):
    """Mark the health monitor pending delete in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, health_mon):
        """Mark the health monitor as pending delete in DB.

        :param health_mon: Health Monitor object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING DELETE in DB for health monitor id: %s",
                  health_mon[constants.HEALTHMONITOR_ID])
        self.health_mon_repo.update(db_apis.get_session(),
                                    health_mon[constants.HEALTHMONITOR_ID],
                                    provisioning_status=(constants.
                                                         PENDING_DELETE))

    def revert(self, health_mon, *args, **kwargs):
        """Mark the health monitor as broken

        :param health_mon: Health Monitor object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark health monitor pending delete in DB "
                    "for health monitor id %s",
                    health_mon[constants.HEALTHMONITOR_ID])
        self.task_utils.mark_health_mon_prov_status_error(
            health_mon[constants.HEALTHMONITOR_ID])


class MarkHealthMonitorPendingUpdateInDB(BaseDatabaseTask):
    """Mark the health monitor pending update in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, health_mon):
        """Mark the health monitor as pending update in DB.

        :param health_mon: Health Monitor object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING UPDATE in DB for health monitor id: %s",
                  health_mon[constants.HEALTHMONITOR_ID])
        self.health_mon_repo.update(db_apis.get_session(),
                                    health_mon[constants.HEALTHMONITOR_ID],
                                    provisioning_status=(constants.
                                                         PENDING_UPDATE))

    def revert(self, health_mon, *args, **kwargs):
        """Mark the health monitor as broken

        :param health_mon: Health Monitor object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark health monitor pending update in DB "
                    "for health monitor id %s",
                    health_mon[constants.HEALTHMONITOR_ID])
        self.task_utils.mark_health_mon_prov_status_error(
            health_mon[constants.HEALTHMONITOR_ID])


class MarkL7PolicyActiveInDB(BaseDatabaseTask):
    """Mark the l7policy ACTIVE in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, l7policy):
        """Mark the l7policy ACTIVE in DB.

        :param l7policy: L7Policy object to be updated
        :returns: None
        """

        LOG.debug("Mark ACTIVE in DB for l7policy id: %s",
                  l7policy[constants.L7POLICY_ID])
        db_l7policy = self.l7policy_repo.get(
            db_apis.get_session(), id=l7policy[constants.L7POLICY_ID])
        op_status = (constants.ONLINE if db_l7policy.enabled
                     else constants.OFFLINE)
        self.l7policy_repo.update(db_apis.get_session(),
                                  l7policy[constants.L7POLICY_ID],
                                  provisioning_status=constants.ACTIVE,
                                  operating_status=op_status)

    def revert(self, l7policy, *args, **kwargs):
        """Mark the l7policy as broken

        :param l7policy: L7Policy object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark l7policy ACTIVE in DB "
                    "for l7policy id %s", l7policy[constants.L7POLICY_ID])
        self.task_utils.mark_l7policy_prov_status_error(
            l7policy[constants.L7POLICY_ID])


class MarkL7PolicyPendingCreateInDB(BaseDatabaseTask):
    """Mark the l7policy pending create in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, l7policy):
        """Mark the l7policy as pending create in DB.

        :param l7policy: L7Policy object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING CREATE in DB for l7policy id: %s",
                  l7policy[constants.L7POLICY_ID])
        self.l7policy_repo.update(db_apis.get_session(),
                                  l7policy[constants.L7POLICY_ID],
                                  provisioning_status=constants.PENDING_CREATE)

    def revert(self, l7policy, *args, **kwargs):
        """Mark the l7policy as broken

        :param l7policy: L7Policy object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark l7policy pending create in DB "
                    "for l7policy id %s", l7policy[constants.L7POLICY_ID])
        self.task_utils.mark_l7policy_prov_status_error(
            l7policy[constants.L7POLICY_ID])


class MarkL7PolicyPendingDeleteInDB(BaseDatabaseTask):
    """Mark the l7policy pending delete in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, l7policy):
        """Mark the l7policy as pending delete in DB.

        :param l7policy: L7Policy object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING DELETE in DB for l7policy id: %s",
                  l7policy[constants.L7POLICY_ID])
        self.l7policy_repo.update(db_apis.get_session(),
                                  l7policy[constants.L7POLICY_ID],
                                  provisioning_status=constants.PENDING_DELETE)

    def revert(self, l7policy, *args, **kwargs):
        """Mark the l7policy as broken

        :param l7policy: L7Policy object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark l7policy pending delete in DB "
                    "for l7policy id %s", l7policy[constants.L7POLICY_ID])
        self.task_utils.mark_l7policy_prov_status_error(
            l7policy[constants.L7POLICY_ID])


class MarkL7PolicyPendingUpdateInDB(BaseDatabaseTask):
    """Mark the l7policy pending update in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, l7policy):
        """Mark the l7policy as pending update in DB.

        :param l7policy: L7Policy object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING UPDATE in DB for l7policy id: %s",
                  l7policy[constants.L7POLICY_ID])
        self.l7policy_repo.update(db_apis.get_session(),
                                  l7policy[constants.L7POLICY_ID],
                                  provisioning_status=(constants.
                                                       PENDING_UPDATE))

    def revert(self, l7policy, *args, **kwargs):
        """Mark the l7policy as broken

        :param l7policy: L7Policy object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark l7policy pending update in DB "
                    "for l7policy id %s", l7policy[constants.L7POLICY_ID])
        self.task_utils.mark_l7policy_prov_status_error(
            l7policy[constants.L7POLICY_ID])


class MarkL7RuleActiveInDB(BaseDatabaseTask):
    """Mark the l7rule ACTIVE in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, l7rule):
        """Mark the l7rule ACTIVE in DB.

        :param l7rule: L7Rule object to be updated
        :returns: None
        """

        LOG.debug("Mark ACTIVE in DB for l7rule id: %s",
                  l7rule[constants.L7RULE_ID])
        db_rule = self.l7rule_repo.get(db_apis.get_session(),
                                       id=l7rule[constants.L7RULE_ID])
        op_status = (constants.ONLINE if db_rule.enabled
                     else constants.OFFLINE)
        self.l7rule_repo.update(db_apis.get_session(),
                                l7rule[constants.L7RULE_ID],
                                provisioning_status=constants.ACTIVE,
                                operating_status=op_status)

    def revert(self, l7rule, *args, **kwargs):
        """Mark the l7rule as broken

        :param l7rule: L7Rule object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark l7rule ACTIVE in DB "
                    "for l7rule id %s", l7rule[constants.L7RULE_ID])
        self.task_utils.mark_l7rule_prov_status_error(
            l7rule[constants.L7RULE_ID])


class MarkL7RulePendingCreateInDB(BaseDatabaseTask):
    """Mark the l7rule pending create in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, l7rule):
        """Mark the l7rule as pending create in DB.

        :param l7rule: L7Rule object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING CREATE in DB for l7rule id: %s",
                  l7rule[constants.L7RULE_ID])
        self.l7rule_repo.update(db_apis.get_session(),
                                l7rule[constants.L7RULE_ID],
                                provisioning_status=constants.PENDING_CREATE)

    def revert(self, l7rule, *args, **kwargs):
        """Mark the l7rule as broken

        :param l7rule: L7Rule object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark l7rule pending create in DB "
                    "for l7rule id %s", l7rule[constants.L7RULE_ID])
        self.task_utils.mark_l7rule_prov_status_error(
            l7rule[constants.L7RULE_ID])


class MarkL7RulePendingDeleteInDB(BaseDatabaseTask):
    """Mark the l7rule pending delete in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, l7rule):
        """Mark the l7rule as pending delete in DB.

        :param l7rule: L7Rule object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING DELETE in DB for l7rule id: %s",
                  l7rule[constants.L7RULE_ID])
        self.l7rule_repo.update(db_apis.get_session(),
                                l7rule[constants.L7RULE_ID],
                                provisioning_status=constants.PENDING_DELETE)

    def revert(self, l7rule, *args, **kwargs):
        """Mark the l7rule as broken

        :param l7rule: L7Rule object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark l7rule pending delete in DB "
                    "for l7rule id %s", l7rule[constants.L7RULE_ID])
        self.task_utils.mark_l7rule_prov_status_error(
            l7rule[constants.L7RULE_ID])


class MarkL7RulePendingUpdateInDB(BaseDatabaseTask):
    """Mark the l7rule pending update in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, l7rule):
        """Mark the l7rule as pending update in DB.

        :param l7rule: L7Rule object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING UPDATE in DB for l7rule id: %s",
                  l7rule[constants.L7RULE_ID])
        self.l7rule_repo.update(db_apis.get_session(),
                                l7rule[constants.L7RULE_ID],
                                provisioning_status=constants.PENDING_UPDATE)

    def revert(self, l7rule, *args, **kwargs):
        """Mark the l7rule as broken

        :param l7rule: L7Rule object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark l7rule pending update in DB "
                    "for l7rule id %s", l7rule[constants.L7RULE_ID])
        self.task_utils.mark_l7rule_prov_status_error(
            l7rule[constants.L7RULE_ID])


class MarkMemberActiveInDB(BaseDatabaseTask):
    """Mark the member ACTIVE in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, member):
        """Mark the member ACTIVE in DB.

        :param member: Member object to be updated
        :returns: None
        """

        LOG.debug("Mark ACTIVE in DB for member id: %s",
                  member[constants.MEMBER_ID])
        self.member_repo.update(db_apis.get_session(),
                                member[constants.MEMBER_ID],
                                provisioning_status=constants.ACTIVE)

    def revert(self, member, *args, **kwargs):
        """Mark the member as broken

        :param member: Member object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark member ACTIVE in DB "
                    "for member id %s", member[constants.MEMBER_ID])
        self.task_utils.mark_member_prov_status_error(
            member[constants.MEMBER_ID])


class MarkMemberPendingCreateInDB(BaseDatabaseTask):
    """Mark the member pending create in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, member):
        """Mark the member as pending create in DB.

        :param member: Member object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING CREATE in DB for member id: %s",
                  member[constants.MEMBER_ID])
        self.member_repo.update(db_apis.get_session(),
                                member[constants.MEMBER_ID],
                                provisioning_status=constants.PENDING_CREATE)

    def revert(self, member, *args, **kwargs):
        """Mark the member as broken

        :param member: Member object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark member pending create in DB "
                    "for member id %s", member[constants.MEMBER_ID])
        self.task_utils.mark_member_prov_status_error(
            member[constants.MEMBER_ID])


class MarkMemberPendingDeleteInDB(BaseDatabaseTask):
    """Mark the member pending delete in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, member):
        """Mark the member as pending delete in DB.

        :param member: Member object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING DELETE in DB for member id: %s",
                  member[constants.MEMBER_ID])
        self.member_repo.update(db_apis.get_session(),
                                member[constants.MEMBER_ID],
                                provisioning_status=constants.PENDING_DELETE)

    def revert(self, member, *args, **kwargs):
        """Mark the member as broken

        :param member: Member object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark member pending delete in DB "
                    "for member id %s", member[constants.MEMBER_ID])
        self.task_utils.mark_member_prov_status_error(
            member[constants.MEMBER_ID])


class MarkMemberPendingUpdateInDB(BaseDatabaseTask):
    """Mark the member pending update in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, member):
        """Mark the member as pending update in DB.

        :param member: Member object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING UPDATE in DB for member id: %s",
                  member[constants.MEMBER_ID])
        self.member_repo.update(db_apis.get_session(),
                                member[constants.MEMBER_ID],
                                provisioning_status=constants.PENDING_UPDATE)

    def revert(self, member, *args, **kwargs):
        """Mark the member as broken

        :param member: Member object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark member pending update in DB "
                    "for member id %s", member[constants.MEMBER_ID])
        self.task_utils.mark_member_prov_status_error(
            member[constants.MEMBER_ID])


class MarkPoolActiveInDB(BaseDatabaseTask):
    """Mark the pool ACTIVE in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, pool_id):
        """Mark the pool ACTIVE in DB.

        :param pool_id: pool_id to be updated
        :returns: None
        """

        LOG.debug("Mark ACTIVE in DB for pool id: %s",
                  pool_id)
        self.pool_repo.update(db_apis.get_session(),
                              pool_id,
                              provisioning_status=constants.ACTIVE)

    def revert(self, pool_id, *args, **kwargs):
        """Mark the pool as broken

        :param pool_id: pool_id that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark pool ACTIVE in DB for pool id %s",
                    pool_id)
        self.task_utils.mark_pool_prov_status_error(pool_id)


class MarkPoolPendingCreateInDB(BaseDatabaseTask):
    """Mark the pool pending create in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, pool_id):
        """Mark the pool as pending create in DB.

        :param pool_id: pool_id of pool object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING CREATE in DB for pool id: %s",
                  pool_id)
        self.pool_repo.update(db_apis.get_session(),
                              pool_id,
                              provisioning_status=constants.PENDING_CREATE)

    def revert(self, pool_id, *args, **kwargs):
        """Mark the pool as broken

        :param pool_id: pool_id of pool object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark pool pending create in DB "
                    "for pool id %s", pool_id)
        self.task_utils.mark_pool_prov_status_error(pool_id)


class MarkPoolPendingDeleteInDB(BaseDatabaseTask):
    """Mark the pool pending delete in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, pool_id):
        """Mark the pool as pending delete in DB.

        :param pool_id: pool_id of pool object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING DELETE in DB for pool id: %s",
                  pool_id)
        self.pool_repo.update(db_apis.get_session(),
                              pool_id,
                              provisioning_status=constants.PENDING_DELETE)

    def revert(self, pool_id, *args, **kwargs):
        """Mark the pool as broken

        :param pool_id: pool_id of pool object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark pool pending delete in DB "
                    "for pool id %s", pool_id)
        self.task_utils.mark_pool_prov_status_error(pool_id)


class MarkPoolPendingUpdateInDB(BaseDatabaseTask):
    """Mark the pool pending update in the DB.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, pool_id):
        """Mark the pool as pending update in DB.

        :param pool_id: pool_id of pool object to be updated
        :returns: None
        """

        LOG.debug("Mark PENDING UPDATE in DB for pool id: %s",
                  pool_id)
        self.pool_repo.update(db_apis.get_session(),
                              pool_id,
                              provisioning_status=constants.PENDING_UPDATE)

    def revert(self, pool_id, *args, **kwargs):
        """Mark the pool as broken

        :param pool_id: pool_id of pool object that failed to update
        :returns: None
        """

        LOG.warning("Reverting mark pool pending update in DB "
                    "for pool id %s", pool_id)
        self.task_utils.mark_pool_prov_status_error(pool_id)


class DecrementHealthMonitorQuota(BaseDatabaseTask):
    """Decrements the health monitor quota for a project.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, project_id):
        """Decrements the health monitor quota.

        :param project_id: The project_id to decrement the quota on.
        :returns: None
        """

        LOG.debug("Decrementing health monitor quota for "
                  "project: %s ", project_id)

        lock_session = db_apis.get_session(autocommit=False)
        try:
            self.repos.decrement_quota(lock_session,
                                       data_models.HealthMonitor,
                                       project_id)
            lock_session.commit()
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error('Failed to decrement health monitor quota for '
                          'project: %(proj)s the project may have excess '
                          'quota in use.', {'proj': project_id})
                lock_session.rollback()

    def revert(self, project_id, result, *args, **kwargs):
        """Re-apply the quota

        :param project_id: The project_id to decrement the quota on.
        :returns: None
        """

        LOG.warning('Reverting decrement quota for health monitor on project'
                    ' %(proj)s Project quota counts may be incorrect.',
                    {'proj': project_id})

        # Increment the quota back if this task wasn't the failure
        if not isinstance(result, failure.Failure):

            try:
                session = db_apis.get_session()
                lock_session = db_apis.get_session(autocommit=False)
                try:
                    self.repos.check_quota_met(session,
                                               lock_session,
                                               data_models.HealthMonitor,
                                               project_id)
                    lock_session.commit()
                except Exception:
                    lock_session.rollback()
            except Exception:
                # Don't fail the revert flow
                pass


class DecrementListenerQuota(BaseDatabaseTask):
    """Decrements the listener quota for a project.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, project_id):
        """Decrements the listener quota.

        :param project_id: The project_id to decrement the quota on.
        :returns: None
        """

        LOG.debug("Decrementing listener quota for "
                  "project: %s ", project_id)

        lock_session = db_apis.get_session(autocommit=False)
        try:
            self.repos.decrement_quota(lock_session,
                                       data_models.Listener,
                                       project_id)
            lock_session.commit()
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error('Failed to decrement listener quota for project: '
                          '%(proj)s the project may have excess quota in use.',
                          {'proj': project_id})
                lock_session.rollback()

    def revert(self, project_id, result, *args, **kwargs):
        """Re-apply the quota

        :param project_id: The project_id to decrement the quota on.
        :returns: None
        """
        LOG.warning('Reverting decrement quota for listener on project '
                    '%(proj)s Project quota counts may be incorrect.',
                    {'proj': project_id})

        # Increment the quota back if this task wasn't the failure
        if not isinstance(result, failure.Failure):

            try:
                session = db_apis.get_session()
                lock_session = db_apis.get_session(autocommit=False)
                try:
                    self.repos.check_quota_met(session,
                                               lock_session,
                                               data_models.Listener,
                                               project_id)
                    lock_session.commit()
                except Exception:
                    lock_session.rollback()
            except Exception:
                # Don't fail the revert flow
                pass


class DecrementLoadBalancerQuota(BaseDatabaseTask):
    """Decrements the load balancer quota for a project.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, project_id):
        """Decrements the load balancer quota.

        :param project_id: Project id where quota should be reduced
        :returns: None
        """

        LOG.debug("Decrementing load balancer quota for "
                  "project: %s ", project_id)

        lock_session = db_apis.get_session(autocommit=False)
        try:
            self.repos.decrement_quota(lock_session,
                                       data_models.LoadBalancer,
                                       project_id)
            lock_session.commit()
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error('Failed to decrement load balancer quota for '
                          'project: %(proj)s the project may have excess '
                          'quota in use.',
                          {'proj': project_id})
                lock_session.rollback()

    def revert(self, project_id, result, *args, **kwargs):
        """Re-apply the quota

        :param project_id: The project id to decrement the quota on.
        :returns: None
        """

        LOG.warning('Reverting decrement quota for load balancer on project '
                    '%(proj)s Project quota counts may be incorrect.',
                    {'proj': project_id})

        # Increment the quota back if this task wasn't the failure
        if not isinstance(result, failure.Failure):

            try:
                session = db_apis.get_session()
                lock_session = db_apis.get_session(autocommit=False)
                try:
                    self.repos.check_quota_met(session,
                                               lock_session,
                                               data_models.LoadBalancer,
                                               project_id)
                    lock_session.commit()
                except Exception:
                    lock_session.rollback()
            except Exception:
                # Don't fail the revert flow
                pass


class DecrementMemberQuota(BaseDatabaseTask):
    """Decrements the member quota for a project.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, project_id):
        """Decrements the member quota.

        :param member: The member to decrement the quota on.
        :returns: None
        """

        LOG.debug("Decrementing member quota for "
                  "project: %s ", project_id)

        lock_session = db_apis.get_session(autocommit=False)
        try:
            self.repos.decrement_quota(lock_session,
                                       data_models.Member,
                                       project_id)
            lock_session.commit()
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error('Failed to decrement member quota for project: '
                          '%(proj)s the project may have excess quota in use.',
                          {'proj': project_id})
                lock_session.rollback()

    def revert(self, project_id, result, *args, **kwargs):
        """Re-apply the quota

        :param member: The member to decrement the quota on.
        :returns: None
        """

        LOG.warning('Reverting decrement quota for member on project %(proj)s '
                    'Project quota counts may be incorrect.',
                    {'proj': project_id})

        # Increment the quota back if this task wasn't the failure
        if not isinstance(result, failure.Failure):

            try:
                session = db_apis.get_session()
                lock_session = db_apis.get_session(autocommit=False)
                try:
                    self.repos.check_quota_met(session,
                                               lock_session,
                                               data_models.Member,
                                               project_id)
                    lock_session.commit()
                except Exception:
                    lock_session.rollback()
            except Exception:
                # Don't fail the revert flow
                pass


class DecrementPoolQuota(BaseDatabaseTask):
    """Decrements the pool quota for a project.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, project_id, pool_child_count):
        """Decrements the pool quota.

        :param project_id: project_id where the pool to decrement the quota on
        :returns: None
        """

        LOG.debug("Decrementing pool quota for "
                  "project: %s ", project_id)

        lock_session = db_apis.get_session(autocommit=False)
        try:
            self.repos.decrement_quota(lock_session,
                                       data_models.Pool,
                                       project_id)

            # Pools cascade delete members and health monitors
            # update the quota for those items as well.
            if pool_child_count['HM'] > 0:
                self.repos.decrement_quota(lock_session,
                                           data_models.HealthMonitor,
                                           project_id)
            if pool_child_count['member'] > 0:
                self.repos.decrement_quota(
                    lock_session, data_models.Member,
                    project_id, quantity=pool_child_count['member'])

            lock_session.commit()
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error('Failed to decrement pool quota for project: '
                          '%(proj)s the project may have excess quota in use.',
                          {'proj': project_id})
                lock_session.rollback()

    def revert(self, project_id, pool_child_count, result, *args, **kwargs):
        """Re-apply the quota

        :param project_id: The id of project to decrement the quota on
        :returns: None
        """

        LOG.warning('Reverting decrement quota for pool on project %(proj)s '
                    'Project quota counts may be incorrect.',
                    {'proj': project_id})

        # Increment the quota back if this task wasn't the failure
        if not isinstance(result, failure.Failure):

            # These are all independent to maximize the correction
            # in case other quota actions have occurred
            try:
                session = db_apis.get_session()
                lock_session = db_apis.get_session(autocommit=False)
                try:
                    self.repos.check_quota_met(session,
                                               lock_session,
                                               data_models.Pool,
                                               project_id)
                    lock_session.commit()
                except Exception:
                    lock_session.rollback()

                # Attempt to increment back the health monitor quota
                if pool_child_count['HM'] > 0:
                    lock_session = db_apis.get_session(autocommit=False)
                    try:
                        self.repos.check_quota_met(session,
                                                   lock_session,
                                                   data_models.HealthMonitor,
                                                   project_id)
                        lock_session.commit()
                    except Exception:
                        lock_session.rollback()

                # Attempt to increment back the member quota
                # This is separate calls to maximize the correction
                # should other factors have increased the in use quota
                # before this point in the revert flow
                for i in range(pool_child_count['member']):
                    lock_session = db_apis.get_session(autocommit=False)
                    try:
                        self.repos.check_quota_met(session,
                                                   lock_session,
                                                   data_models.Member,
                                                   project_id)
                        lock_session.commit()
                    except Exception:
                        lock_session.rollback()
            except Exception:
                # Don't fail the revert flow
                pass


class CountPoolChildrenForQuota(BaseDatabaseTask):
    """Counts the pool child resources for quota management.

    Since the children of pools are cleaned up by the sqlalchemy
    cascade delete settings, we need to collect the quota counts
    for the child objects early.

    """

    def execute(self, pool_id):
        """Count the pool child resources for quota management

        :param pool_id: pool_id of pool object to count children on
        :returns: None
        """
        session = db_apis.get_session()
        hm_count, member_count = (
            self.pool_repo.get_children_count(session, pool_id))

        return {'HM': hm_count, 'member': member_count}


class DecrementL7policyQuota(BaseDatabaseTask):
    """Decrements the l7policy quota for a project.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, l7policy):
        """Decrements the l7policy quota.

        :param l7policy: The l7policy to decrement the quota on.
        :returns: None
        """
        LOG.debug("Decrementing l7policy quota for "
                  "project: %s ", l7policy[constants.PROJECT_ID])
        lock_session = db_apis.get_session(autocommit=False)
        try:
            self.repos.decrement_quota(lock_session,
                                       data_models.L7Policy,
                                       l7policy[constants.PROJECT_ID])
            db_l7policy = self.l7policy_repo.get(
                db_apis.get_session(), id=l7policy[constants.L7POLICY_ID])

            if db_l7policy and db_l7policy.l7rules:
                self.repos.decrement_quota(lock_session,
                                           data_models.L7Rule,
                                           l7policy[constants.PROJECT_ID],
                                           quantity=len(db_l7policy.l7rules))
            lock_session.commit()
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error('Failed to decrement l7policy quota for project: '
                          '%(proj)s the project may have excess quota in use.',
                          {'proj': l7policy[constants.L7POLICY_ID]})
                lock_session.rollback()

    def revert(self, l7policy, result, *args, **kwargs):
        """Re-apply the quota

        :param l7policy: The l7policy to decrement the quota on.
        :returns: None
        """
        LOG.warning('Reverting decrement quota for l7policy on project'
                    ' %(proj)s Project quota counts may be incorrect.',
                    {'proj': l7policy[constants.PROJECT_ID]})
        # Increment the quota back if this task wasn't the failure
        if not isinstance(result, failure.Failure):
            try:
                session = db_apis.get_session()
                lock_session = db_apis.get_session(autocommit=False)
                try:
                    self.repos.check_quota_met(session,
                                               lock_session,
                                               data_models.L7Policy,
                                               l7policy[constants.PROJECT_ID])
                    lock_session.commit()
                except Exception:
                    lock_session.rollback()
                db_l7policy = self.l7policy_repo.get(
                    session, id=l7policy[constants.L7POLICY_ID])
                if db_l7policy:
                    # Attempt to increment back the L7Rule quota
                    for i in range(len(db_l7policy.l7rules)):
                        lock_session = db_apis.get_session(autocommit=False)
                        try:
                            self.repos.check_quota_met(
                                session, lock_session, data_models.L7Rule,
                                db_l7policy.project_id)
                            lock_session.commit()
                        except Exception:
                            lock_session.rollback()
            except Exception:
                # Don't fail the revert flow
                pass


class DecrementL7ruleQuota(BaseDatabaseTask):
    """Decrements the l7rule quota for a project.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, l7rule):
        """Decrements the l7rule quota.

        :param l7rule: The l7rule to decrement the quota on.
        :returns: None
        """

        LOG.debug("Decrementing l7rule quota for "
                  "project: %s ", l7rule[constants.PROJECT_ID])

        lock_session = db_apis.get_session(autocommit=False)
        try:
            self.repos.decrement_quota(lock_session,
                                       data_models.L7Rule,
                                       l7rule[constants.PROJECT_ID])
            lock_session.commit()
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error('Failed to decrement l7rule quota for project: '
                          '%(proj)s the project may have excess quota in use.',
                          {'proj': l7rule[constants.PROJECT_ID]})
                lock_session.rollback()

    def revert(self, l7rule, result, *args, **kwargs):
        """Re-apply the quota

        :param l7rule: The l7rule to decrement the quota on.
        :returns: None
        """

        LOG.warning('Reverting decrement quota for l7rule on project %(proj)s '
                    'Project quota counts may be incorrect.',
                    {'proj': l7rule[constants.PROJECT_ID]})

        # Increment the quota back if this task wasn't the failure
        if not isinstance(result, failure.Failure):

            try:
                session = db_apis.get_session()
                lock_session = db_apis.get_session(autocommit=False)
                try:
                    self.repos.check_quota_met(session,
                                               lock_session,
                                               data_models.L7Rule,
                                               l7rule[constants.PROJECT_ID])
                    lock_session.commit()
                except Exception:
                    lock_session.rollback()
            except Exception:
                # Don't fail the revert flow
                pass


class UpdatePoolMembersOperatingStatusInDB(BaseDatabaseTask):
    """Updates the members of a pool operating status.

    Since sqlalchemy will likely retry by itself always revert if it fails
    """

    def execute(self, pool_id, operating_status):
        """Update the members of a pool operating status in DB.

        :param pool_id: pool_id of pool object to be updated
        :param operating_status: Operating status to set
        :returns: None
        """

        LOG.debug("Updating member operating status to %(status)s in DB for "
                  "pool id: %(pool)s", {'status': operating_status,
                                        'pool': pool_id})
        self.member_repo.update_pool_members(db_apis.get_session(),
                                             pool_id,
                                             operating_status=operating_status)
